"""DuckDB repository implementations for local/dev storage."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pyarrow as pa

from reimagina.indicators.validation import validate_result_rows

from .._utils import normalize_json_value as _normalize_json_value
from .._utils import scenario_filter as _scenario_filter


def _rows_as_dicts(result) -> list[dict]:
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class DuckDBSourceCatalogRepository:
    def __init__(self, connection):
        self.connection = connection

    def register_or_get_source(
        self,
        *,
        name: str,
        provider: str,
        storage_type: str,
        integration_role: str,
        storage_uri: str,
        version: str,
        license: str | None = None,
        notes: str | None = None,
    ) -> str:
        existing = self.get_source(name=name, provider=provider, version=version)
        if existing is not None:
            self.connection.execute(
                """
                UPDATE curated_geo.data_source_catalog
                   SET storage_type = ?,
                       integration_role = ?,
                       storage_uri = ?,
                       license = COALESCE(?, license),
                       notes = COALESCE(?, notes)
                 WHERE id = ?
                """,
                [
                    storage_type,
                    integration_role,
                    storage_uri,
                    license,
                    notes,
                    existing["id"],
                ],
            )
            return str(existing["id"])

        source_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.data_source_catalog
              (id, name, provider, storage_type, integration_role, storage_uri, version, license, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source_id,
                name,
                provider,
                storage_type,
                integration_role,
                storage_uri,
                version,
                license,
                notes,
            ],
        )
        return source_id

    def get_source(self, *, name: str, provider: str, version: str):
        row = self.connection.execute(
            """
            SELECT id, name, provider, storage_type, integration_role, storage_uri, version
            FROM curated_geo.data_source_catalog
            WHERE name = ? AND provider = ? AND version = ?
            """,
            [name, provider, version],
        ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "name": row[1],
            "provider": row[2],
            "storage_type": row[3],
            "integration_role": row[4],
            "storage_uri": row[5],
            "version": row[6],
        }

    def update_source_storage_uri(self, source_id: str, storage_uri: str) -> None:
        self.connection.execute(
            """
            UPDATE curated_geo.data_source_catalog
               SET storage_uri = ?
             WHERE id = ?
            """,
            [storage_uri, source_id],
        )

    def start_ingestion_run(
        self,
        *,
        source_id: str,
        orchestrator: str,
        orchestrator_run_id: str,
        transformation_step: str,
        code_version: str,
    ) -> str:
        run_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.ingestion_run
              (id, source_id, orchestrator, orchestrator_run_id, transformation_step, code_version, status)
            VALUES (?, ?, ?, ?, ?, ?, 'running')
            """,
            [
                run_id,
                source_id,
                orchestrator,
                orchestrator_run_id,
                transformation_step,
                code_version,
            ],
        )
        return run_id

    def complete_ingestion_run(
        self, run_id: str, *, status: str, details: dict | None = None
    ) -> None:
        self.connection.execute(
            """
            UPDATE curated_geo.ingestion_run
               SET status = ?, completed_at = now(), details = CAST(? AS JSON)
             WHERE id = ?
            """,
            [status, json.dumps(details or {}), run_id],
        )


class DuckDBFeatureRepository:
    def __init__(self, connection):
        self.connection = connection

    def seed_feature_types(self, rows: list[tuple]) -> None:
        if not rows:
            return
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO curated_geo.feature_type_catalog
              (context, type_slug, label, geometry_family, role, admin_level)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [list(row) for row in rows],
        )

    def get_type_id(self, context: str, type_slug: str) -> str:
        row = self.connection.execute(
            """
            SELECT id FROM curated_geo.feature_type_catalog
            WHERE context = ? AND type_slug = ?
            """,
            [context, type_slug],
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown feature type: {context!r}/{type_slug!r}")
        return str(row[0])

    def bulk_insert_features(
        self,
        gdf: gpd.GeoDataFrame,
        *,
        source_id: str,
        ingestion_run_id: str,
        type_id: str,
        dataset_version: str,
        source_record_ref_col: str,
        name_col: str | None = None,
        attributes_cols: list[str] | None = None,
        parent_id_col: str | None = None,
    ) -> int:
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)

        type_row = self.connection.execute(
            """
            SELECT type_slug, role FROM curated_geo.feature_type_catalog WHERE id = ?
            """,
            [type_id],
        ).fetchone()
        if type_row is None:
            raise ValueError(f"Unknown feature type id: {type_id!r}")
        type_slug, role = type_row

        rows: list[dict] = []
        for _, row in gdf.iterrows():
            attributes: dict = {}
            if attributes_cols:
                for col in attributes_cols:
                    if col in row.index and row[col] is not None:
                        attributes[col] = _normalize_json_value(row[col])

            rows.append(
                {
                    "id": str(uuid4()),
                    "type_id": type_id,
                    "type_slug": type_slug,
                    "role": role,
                    "parent_id": str(row[parent_id_col])
                    if parent_id_col and parent_id_col in row.index
                    else None,
                    "name": str(row[name_col])
                    if name_col and name_col in row.index
                    else None,
                    "geometry_wkt": row.geometry.wkt,
                    "attributes_json": json.dumps(attributes, allow_nan=False),
                    "source_id": source_id,
                    "source_record_ref": str(row[source_record_ref_col]),
                    "ingestion_run_id": ingestion_run_id,
                    "dataset_version": dataset_version,
                }
            )

        if not rows:
            return 0

        source_frame = pa.Table.from_pylist(rows)
        relation_name = f"feature_load_{uuid4().hex}"
        self.connection.register(relation_name, source_frame)
        try:
            inserted = self.connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {relation_name} s
                LEFT JOIN curated_geo.feature f
                  ON f.source_id = s.source_id
                 AND f.dataset_version = s.dataset_version
                 AND f.source_record_ref = s.source_record_ref
                WHERE f.id IS NULL
                """
            ).fetchone()[0]

            self.connection.execute(
                f"""
                INSERT INTO curated_geo.feature
                  (id, type_id, type_slug, role, parent_id, name, geometry, attributes,
                   source_id, source_record_ref, ingestion_run_id, dataset_version)
                SELECT
                  s.id,
                  s.type_id,
                  s.type_slug,
                  s.role,
                  s.parent_id,
                  s.name,
                  ST_GeomFromText(s.geometry_wkt),
                  CAST(s.attributes_json AS JSON),
                  s.source_id,
                  s.source_record_ref,
                  s.ingestion_run_id,
                  s.dataset_version
                FROM {relation_name} s
                LEFT JOIN curated_geo.feature f
                  ON f.source_id = s.source_id
                 AND f.dataset_version = s.dataset_version
                 AND f.source_record_ref = s.source_record_ref
                WHERE f.id IS NULL
                """
            )
        finally:
            self.connection.unregister(relation_name)
        return int(inserted)

    def bulk_insert_features_from_parquet(
        self,
        *,
        parquet_path: str | Path,
        source_id: str,
        ingestion_run_id: str,
        type_id: str,
        dataset_version: str,
        source_record_ref_col: str,
        name_col: str | None = None,
        attributes_cols: list[str] | None = None,
        parent_id_col: str | None = None,
    ) -> int:
        """Insert features directly from a GeoParquet file inside DuckDB.

        This avoids materializing millions of rows in Python just to insert them
        back into DuckDB.
        """
        type_row = self.connection.execute(
            """
            SELECT type_slug, role FROM curated_geo.feature_type_catalog WHERE id = ?
            """,
            [type_id],
        ).fetchone()
        if type_row is None:
            raise ValueError(f"Unknown feature type id: {type_id!r}")
        type_slug, role = type_row

        path = str(Path(parquet_path))
        available_columns = {
            row[0]
            for row in self.connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                [path],
            ).fetchall()
        }
        if source_record_ref_col not in available_columns:
            raise ValueError(
                f"Required source record column {source_record_ref_col!r} is missing from {path!r}"
            )

        source_record_ref_ident = _quote_identifier(source_record_ref_col)
        attr_expr = "'{}'"
        if attributes_cols:
            present_attr_cols = [
                col for col in attributes_cols if col in available_columns
            ]
        else:
            present_attr_cols = []
        if present_attr_cols:
            fields = ", ".join(
                f"{_quote_identifier(col)} := s.{_quote_identifier(col)}"
                for col in present_attr_cols
            )
            attr_expr = f"to_json(struct_pack({fields}))"
        if name_col and name_col in available_columns:
            name_expr = f"CAST(s.{_quote_identifier(name_col)} AS VARCHAR)"
        else:
            name_expr = "NULL"
        if parent_id_col and parent_id_col in available_columns:
            parent_expr = f"TRY_CAST(s.{_quote_identifier(parent_id_col)} AS UUID)"
        else:
            parent_expr = "NULL"

        count_sql = f"""
            SELECT COUNT(*)
            FROM read_parquet(?) s
            LEFT JOIN curated_geo.feature f
              ON f.source_id = ?
             AND f.dataset_version = ?
             AND f.source_record_ref = CAST(s.{source_record_ref_ident} AS VARCHAR)
            WHERE f.id IS NULL
        """
        inserted = self.connection.execute(
            count_sql,
            [path, source_id, dataset_version],
        ).fetchone()[0]

        insert_sql = f"""
            INSERT INTO curated_geo.feature
              (id, type_id, type_slug, role, parent_id, name, geometry, attributes,
               source_id, source_record_ref, ingestion_run_id, dataset_version)
            SELECT
              uuid(),
              ?,
              ?,
              ?,
              {parent_expr},
              {name_expr},
              s.geometry,
              CAST({attr_expr} AS JSON),
              ?,
              CAST(s.{source_record_ref_ident} AS VARCHAR),
              ?,
              ?
            FROM read_parquet(?) s
            LEFT JOIN curated_geo.feature f
              ON f.source_id = ?
             AND f.dataset_version = ?
             AND f.source_record_ref = CAST(s.{source_record_ref_ident} AS VARCHAR)
            WHERE f.id IS NULL
        """
        self.connection.execute(
            insert_sql,
            [
                type_id,
                type_slug,
                role,
                source_id,
                ingestion_run_id,
                dataset_version,
                path,
                source_id,
                dataset_version,
            ],
        )
        return int(inserted)

    def geodesic_metrics_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[tuple[str, float, float]]:
        # DuckDB has no geography type; project to UTM 14N (EPSG:32614 —
        # CDMX's local UTM zone) and measure in planar metres. Accurate
        # enough at city scale for urban-form indicators. `always_xy=true`
        # keeps coordinate order stable across ST_Transform's flipped
        # default on DuckDB-spatial. feature_ids narrows to the
        # selective-recompute set via list_contains.
        id_filter = (
            " AND list_contains(?, CAST(f.id AS VARCHAR))" if feature_ids else ""
        )
        if aoi_id is None:
            params: list = [type_id]
            if feature_ids:
                params.append(list(feature_ids))
            rows = self.connection.execute(
                f"""
                SELECT CAST(f.id AS VARCHAR),
                       ST_Area(ST_Transform(f.geometry, 'EPSG:4326', 'EPSG:32614', true)),
                       ST_Perimeter(ST_Transform(f.geometry, 'EPSG:4326', 'EPSG:32614', true))
                FROM curated_geo.feature f
                WHERE CAST(f.type_id AS VARCHAR) = ?{id_filter}
                """,
                params,
            ).fetchall()
        else:
            params = [aoi_id, type_id]
            if feature_ids:
                params.append(list(feature_ids))
            rows = self.connection.execute(
                f"""
                SELECT CAST(f.id AS VARCHAR),
                       ST_Area(ST_Transform(f.geometry, 'EPSG:4326', 'EPSG:32614', true)),
                       ST_Perimeter(ST_Transform(f.geometry, 'EPSG:4326', 'EPSG:32614', true))
                FROM curated_geo.feature f
                JOIN curated_geo.area_of_interest aoi
                  ON CAST(aoi.id AS VARCHAR) = ?
                  AND ST_Intersects(f.geometry, aoi.geometry)
                WHERE CAST(f.type_id AS VARCHAR) = ?{id_filter}
                """,
                params,
            ).fetchall()
        return [(str(fid), float(area), float(perim)) for fid, area, perim in rows]

    def iter_geometries_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        if aoi_id is None:
            rows = self.connection.execute(
                """
                SELECT CAST(f.id AS VARCHAR), ST_AsWKB(f.geometry)
                FROM curated_geo.feature f
                WHERE CAST(f.type_id AS VARCHAR) = ?
                """,
                [type_id],
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT CAST(f.id AS VARCHAR), ST_AsWKB(f.geometry)
                FROM curated_geo.feature f
                JOIN curated_geo.area_of_interest aoi
                  ON CAST(aoi.id AS VARCHAR) = ?
                  AND ST_Intersects(f.geometry, aoi.geometry)
                WHERE CAST(f.type_id AS VARCHAR) = ?
                """,
                [aoi_id, type_id],
            ).fetchall()
        return [(str(fid), bytes(wkb)) for fid, wkb in rows]

    def find_by_attributes(
        self,
        *,
        type_slug: str,
        attributes: dict[str, str],
    ) -> dict[str, str] | None:
        if not attributes:
            raise ValueError("find_by_attributes requires at least one attribute")
        predicates = [
            f"json_extract_string(attributes, '$.{key}') = ?" for key in attributes
        ]
        params: list = list(attributes.values()) + [type_slug]
        row = self.connection.execute(
            f"""
            SELECT CAST(id AS VARCHAR), ST_AsText(geometry)
            FROM curated_geo.feature
            WHERE {' AND '.join(predicates)}
              AND type_slug = ?
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        return {"id": str(row[0]), "geometry_wkt": str(row[1])}

    def centroid_in_parent_counts(
        self,
        *,
        parent_type_id: str,
        child_type_id: str,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[tuple[str, int, float]]:
        # feature_ids restricts the *parents* to the selective-recompute set.
        id_filter = (
            " AND list_contains(?, CAST(parent.id AS VARCHAR))" if feature_ids else ""
        )
        if aoi_id is None:
            params: list = [child_type_id, parent_type_id]
            if feature_ids:
                params.append(list(feature_ids))
            rows = self.connection.execute(
                f"""
                SELECT CAST(parent.id AS VARCHAR),
                       COUNT(child.id),
                       ST_Area(ST_Transform(parent.geometry, 'EPSG:4326', 'EPSG:32614', true))
                FROM curated_geo.feature parent
                LEFT JOIN curated_geo.feature child
                       ON CAST(child.type_id AS VARCHAR) = ?
                      AND ST_Intersects(parent.geometry, child.geometry)
                      AND ST_Within(ST_Centroid(child.geometry), parent.geometry)
                WHERE CAST(parent.type_id AS VARCHAR) = ?{id_filter}
                GROUP BY parent.id, parent.geometry
                """,
                params,
            ).fetchall()
        else:
            params = [aoi_id, child_type_id, parent_type_id]
            if feature_ids:
                params.append(list(feature_ids))
            rows = self.connection.execute(
                f"""
                SELECT CAST(parent.id AS VARCHAR),
                       COUNT(child.id),
                       ST_Area(ST_Transform(parent.geometry, 'EPSG:4326', 'EPSG:32614', true))
                FROM curated_geo.feature parent
                JOIN curated_geo.area_of_interest aoi
                  ON CAST(aoi.id AS VARCHAR) = ?
                  AND ST_Intersects(parent.geometry, aoi.geometry)
                LEFT JOIN curated_geo.feature child
                       ON CAST(child.type_id AS VARCHAR) = ?
                      AND ST_Intersects(parent.geometry, child.geometry)
                      AND ST_Within(ST_Centroid(child.geometry), parent.geometry)
                WHERE CAST(parent.type_id AS VARCHAR) = ?{id_filter}
                GROUP BY parent.id, parent.geometry
                """,
                params,
            ).fetchall()
        return [(str(fid), int(count), float(area)) for fid, count, area in rows]


class DuckDBObservationRepository:
    def __init__(self, connection):
        self.connection = connection

    def upsert_current(
        self,
        *,
        feature_id: str,
        attribute_key: str,
        value_type: str,
        value: object,
        source_id: str,
        ingestion_run_id: str,
        lineage_ref: dict | None = None,
    ) -> str:
        value_numeric = value if value_type == "numeric" else None
        value_text = value if value_type == "text" else None
        value_json = json.dumps(value) if value_type == "json" else None
        observation_id = str(uuid4())
        self.connection.execute(
            """
            UPDATE curated_geo.observation
               SET is_current = false, valid_to = now(), updated_at = now()
             WHERE feature_id = ? AND attribute_key = ? AND is_current
            """,
            [feature_id, attribute_key],
        )
        self.connection.execute(
            """
            INSERT INTO curated_geo.observation
              (id, feature_id, attribute_key, value_type,
               value_numeric, value_text, value_json,
               source_id, ingestion_run_id, lineage_ref, is_current)
            VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?, CAST(? AS JSON), true)
            """,
            [
                observation_id,
                feature_id,
                attribute_key,
                value_type,
                value_numeric,
                value_text,
                value_json,
                source_id,
                ingestion_run_id,
                json.dumps(lineage_ref) if lineage_ref else None,
            ],
        )
        return observation_id

    def bulk_upsert_current(
        self, records: list[dict], *, source_id: str, ingestion_run_id: str
    ) -> int:
        if not records:
            return 0

        # Build retire pairs and insert rows in one pass.
        retire_pairs: list[list] = [
            [r["feature_id"], r["attribute_key"]] for r in records
        ]
        insert_rows: list[list] = []
        for r in records:
            value_numeric = r["value"] if r["value_type"] == "numeric" else None
            value_text = r["value"] if r["value_type"] == "text" else None
            value_json = json.dumps(r["value"]) if r["value_type"] == "json" else None
            insert_rows.append(
                [
                    str(uuid4()),
                    r["feature_id"],
                    r["attribute_key"],
                    r["value_type"],
                    value_numeric,
                    value_text,
                    value_json,
                    source_id,
                    ingestion_run_id,
                    json.dumps(r["lineage_ref"]) if r.get("lineage_ref") else None,
                ]
            )

        # Register Arrow tables so we can retire and insert in two SQL statements
        # instead of 2N individual round-trips.
        retire_table = pa.Table.from_pylist(
            [{"fid": p[0], "akey": p[1]} for p in retire_pairs]
        )
        retire_name = f"retire_obs_{uuid4().hex}"
        self.connection.register(retire_name, retire_table)
        try:
            self.connection.execute(f"""
                UPDATE curated_geo.observation
                   SET is_current = false, valid_to = now(), updated_at = now()
                  FROM {retire_name} v
                 WHERE CAST(curated_geo.observation.feature_id AS VARCHAR) = v.fid
                   AND curated_geo.observation.attribute_key = v.akey
                   AND curated_geo.observation.is_current
            """)
        finally:
            self.connection.unregister(retire_name)

        insert_table = pa.Table.from_pylist(
            [
                {
                    "id": row[0],
                    "feature_id": row[1],
                    "attribute_key": row[2],
                    "value_type": row[3],
                    "value_numeric": row[4],
                    "value_text": row[5],
                    "value_json": row[6],
                    "source_id": row[7],
                    "ingestion_run_id": row[8],
                    "lineage_ref": row[9],
                }
                for row in insert_rows
            ]
        )
        insert_name = f"insert_obs_{uuid4().hex}"
        self.connection.register(insert_name, insert_table)
        try:
            self.connection.execute(f"""
                INSERT INTO curated_geo.observation
                  (id, feature_id, attribute_key, value_type,
                   value_numeric, value_text, value_json,
                   source_id, ingestion_run_id, lineage_ref, is_current)
                SELECT
                  CAST(id AS UUID),
                  CAST(feature_id AS UUID),
                  attribute_key,
                  value_type,
                  value_numeric,
                  value_text,
                  CAST(value_json AS JSON),
                  CAST(source_id AS UUID),
                  CAST(ingestion_run_id AS UUID),
                  CAST(lineage_ref AS JSON),
                  true
                FROM {insert_name}
            """)
        finally:
            self.connection.unregister(insert_name)

        return len(records)

    def retire_current_by_prefix(self, *, prefix: str) -> int:
        # Match either the exact prefix OR `<prefix>_<anything>`.
        # Using starts_with() instead of LIKE + ESCAPE sidesteps the
        # backslash-quoting differences between Postgres E-strings and
        # DuckDB's own escape rules — both engines have a straight
        # starts_with() builtin, so the two backends share semantics.
        # Retirement is cross-source by design.
        if not prefix:
            raise ValueError("retire_current_by_prefix: prefix must be non-empty")
        result = self.connection.execute(
            """
            UPDATE curated_geo.observation
               SET is_current = false, valid_to = now(), updated_at = now()
             WHERE is_current
               AND (attribute_key = ?
                    OR starts_with(attribute_key, ? || '_'))
            """,
            [prefix, prefix],
        )
        rowcount = getattr(result, "rowcount", -1)
        if rowcount >= 0:
            return rowcount
        # Older DuckDB builds returned -1; count via a follow-up select.
        row = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM curated_geo.observation
            WHERE NOT is_current
              AND updated_at = valid_to
              AND (attribute_key = ?
                   OR starts_with(attribute_key, ? || '_'))
            """,
            [prefix, prefix],
        ).fetchone()
        return int(row[0]) if row else 0

    def has_active_prefix(
        self,
        *,
        prefix: str,
        excluding_source_id: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1
            FROM curated_geo.observation
            WHERE is_current
              AND CAST(source_id AS VARCHAR) <> ?
              AND (attribute_key = ?
                   OR starts_with(attribute_key, ? || '_'))
            LIMIT 1
            """,
            [excluding_source_id, prefix, prefix],
        ).fetchone()
        return row is not None


class DuckDBFileAssetRepository:
    def __init__(self, connection):
        self.connection = connection

    def register_file_asset(
        self,
        *,
        source_id: str,
        ingestion_run_id: str,
        storage_uri: str,
        dataset_version: str,
        format: str | None = None,
        crs: str | None = None,
        resolution_m: float | None = None,
        band_description: dict | None = None,
        captured_at: datetime | None = None,
    ) -> str:
        asset_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.file_asset
              (id, source_id, ingestion_run_id, storage_uri, format, crs,
               resolution_m, band_description, captured_at, dataset_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?)
            """,
            [
                asset_id,
                source_id,
                ingestion_run_id,
                storage_uri,
                format,
                crs,
                resolution_m,
                json.dumps(band_description or {}),
                captured_at,
                dataset_version,
            ],
        )
        return asset_id

    def get_by_source(self, source_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR),
                   CAST(source_id AS VARCHAR),
                   CAST(ingestion_run_id AS VARCHAR),
                   storage_uri, format, crs, resolution_m,
                   CAST(band_description AS VARCHAR) AS band_description,
                   captured_at, dataset_version, created_at
            FROM curated_geo.file_asset
            WHERE CAST(source_id AS VARCHAR) = ?
            """,
            [source_id],
        ).fetchone()
        if row is None:
            return None
        band_desc = row[7]
        if isinstance(band_desc, str):
            try:
                band_desc = json.loads(band_desc)
            except (TypeError, ValueError):
                band_desc = None
        return {
            "id": row[0],
            "source_id": row[1],
            "ingestion_run_id": row[2],
            "storage_uri": row[3],
            "format": row[4],
            "crs": row[5],
            "resolution_m": row[6],
            "band_description": band_desc,
            "captured_at": row[8],
            "dataset_version": row[9],
            "created_at": row[10],
        }


class DuckDBRasterLayerRepository:
    _LAYER_COLS = (
        "id",
        "source_id",
        "asset_id",
        "layer_slug",
        "logical_name",
        "description",
        "colormap_name",
        "rescale",
        "minzoom",
        "maxzoom",
        "published",
        "aggregation_kind",
        "created_at",
        "storage_uri",
        "crs",
        "resolution_m",
        "format",
        "band_description",
    )

    def __init__(self, connection):
        self.connection = connection

    def register_layer(
        self,
        *,
        source_id: str,
        asset_id: str,
        layer_slug: str,
        logical_name: str | None = None,
        description: str | None = None,
        colormap_name: str | None = None,
        rescale: str | None = None,
        minzoom: int = 5,
        maxzoom: int = 14,
        published: bool = True,
        aggregation_kind: str = "intensive",
    ) -> str:
        layer_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.raster_layer
              (id, source_id, asset_id, layer_slug, logical_name, description,
               colormap_name, rescale, minzoom, maxzoom, published,
               aggregation_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                layer_id,
                source_id,
                asset_id,
                layer_slug,
                logical_name,
                description,
                colormap_name,
                rescale,
                minzoom,
                maxzoom,
                published,
                aggregation_kind,
            ],
        )
        return layer_id

    def get_by_source(self, source_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT id, source_id, asset_id, layer_slug, logical_name,
                   description, colormap_name, rescale, minzoom, maxzoom,
                   published, aggregation_kind, created_at,
                   storage_uri, crs, resolution_m, format, band_description
            FROM serving_geo.raster_layer_v
            WHERE source_id = ?
            """,
            [source_id],
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def get_by_id(self, layer_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT id, source_id, asset_id, layer_slug, logical_name,
                   description, colormap_name, rescale, minzoom, maxzoom,
                   published, aggregation_kind, created_at,
                   storage_uri, crs, resolution_m, format, band_description
            FROM serving_geo.raster_layer_v
            WHERE id = ?
            """,
            [layer_id],
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_published_layers(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT id, source_id, asset_id, layer_slug, logical_name,
                   description, colormap_name, rescale, minzoom, maxzoom,
                   published, aggregation_kind, created_at,
                   storage_uri, crs, resolution_m, format, band_description
            FROM serving_geo.raster_layer_v
            WHERE published
            ORDER BY layer_slug
            """
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def set_published(self, *, layer_slug: str, published: bool) -> None:
        self.connection.execute(
            """
            UPDATE curated_geo.raster_layer
               SET published = ?
             WHERE layer_slug = ?
            """,
            [published, layer_slug],
        )

    @classmethod
    def _row_to_dict(cls, row: tuple) -> dict:
        record = dict(zip(cls._LAYER_COLS, row, strict=False))
        band_desc = record.get("band_description")
        if isinstance(band_desc, str):
            try:
                record["band_description"] = json.loads(band_desc)
            except (TypeError, ValueError):
                record["band_description"] = None
        return record


class DuckDBAggregationRecipeRepository:
    """DuckDB-backed recipe registry (r020). Reads via serving view."""

    _COLS = (
        "id",
        "input_kind",
        "input_raster_layer_id",
        "input_raster_layer_slug",
        "input_attribute_key_prefix",
        "input_feature_type_slug",
        "target_feature_type_slug",
        "attribute_key_prefix",
        "method",
        "weight_basis",
        "params",
        "coverage_threshold",
        "aoi_id",
        "enabled",
        "created_at",
        "updated_at",
    )

    def __init__(self, connection):
        self.connection = connection

    def register_recipe(
        self,
        *,
        input_kind: str,
        target_feature_type_slug: str,
        attribute_key_prefix: str,
        method: str,
        input_raster_layer_id: str | None = None,
        input_attribute_key_prefix: str | None = None,
        input_feature_type_slug: str | None = None,
        weight_basis: str | None = None,
        params: dict | None = None,
        coverage_threshold: float = 0.5,
        aoi_id: str | None = None,
        enabled: bool = True,
    ) -> str:
        recipe_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.aggregation_recipe
              (id, input_kind, input_raster_layer_id, input_attribute_key_prefix,
               input_feature_type_slug, target_feature_type_slug,
               attribute_key_prefix, method, weight_basis, params,
               coverage_threshold, aoi_id, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                recipe_id,
                input_kind,
                input_raster_layer_id,
                input_attribute_key_prefix,
                input_feature_type_slug,
                target_feature_type_slug,
                attribute_key_prefix,
                method,
                weight_basis,
                json.dumps(params or {}),
                coverage_threshold,
                aoi_id,
                enabled,
            ],
        )
        return recipe_id

    def get_recipe(self, recipe_id: str) -> dict | None:
        row = self.connection.execute(
            f"SELECT {', '.join(self._COLS)} "
            f"FROM serving_geo.aggregation_recipe_v WHERE id = ?",
            [recipe_id],
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_recipes(
        self,
        *,
        input_raster_layer_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list = []
        if input_raster_layer_id is not None:
            conditions.append("input_raster_layer_id = ?")
            params.append(input_raster_layer_id)
        if enabled_only:
            conditions.append("enabled")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.connection.execute(
            f"SELECT {', '.join(self._COLS)} "
            f"FROM serving_geo.aggregation_recipe_v{where} "
            f"ORDER BY target_feature_type_slug, attribute_key_prefix",
            params,
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def set_enabled(self, *, recipe_id: str, enabled: bool) -> None:
        self.connection.execute(
            """
            UPDATE curated_geo.aggregation_recipe
               SET enabled = ?, updated_at = now()
             WHERE id = ?
            """,
            [enabled, recipe_id],
        )

    def delete_recipe(self, recipe_id: str) -> None:
        self.connection.execute(
            "DELETE FROM curated_geo.aggregation_recipe WHERE id = ?",
            [recipe_id],
        )

    @classmethod
    def _row_to_dict(cls, row: tuple) -> dict:
        record = dict(zip(cls._COLS, row, strict=False))
        params = record.get("params")
        if isinstance(params, str):
            try:
                record["params"] = json.loads(params)
            except (TypeError, ValueError):
                record["params"] = {}
        return record


class DuckDBAOIRepository:
    def __init__(self, connection):
        self.connection = connection

    def create_aoi(
        self,
        *,
        project_id: str,
        name: str,
        geometry_wkt: str,
        description: str | None = None,
    ) -> str:
        aoi_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.area_of_interest
              (id, project_id, name, description, geometry)
            VALUES (?, ?, ?, ?, ST_GeomFromText(?))
            """,
            [aoi_id, project_id, name, description, geometry_wkt],
        )
        return aoi_id

    def compute_overlaps(self, aoi_id: str) -> int:
        """Populate aoi_feature for the given AOI using geodesic measurements (m² / m).

        DuckDB spatial's ST_Area/ST_Length operate in coordinate units (degrees² / degrees
        for EPSG:4326). To produce metric measurements consistent with the PostgreSQL
        backend, intersection geometries are fetched from DuckDB and measured in Python
        via pyproj.Geod (WGS84 ellipsoid).

        NOTE: count and insert are not a single atomic statement — the returned count
        matches what was inserted but could be inaccurate under concurrent writes
        (accepted prototype limitation). Overlap computation is idempotent via
        DELETE + re-insert.
        """
        import shapely.wkb
        from pyproj import Geod

        geod = Geod(ellps="WGS84")

        self.connection.execute(
            "DELETE FROM curated_geo.aoi_feature WHERE aoi_id = ?", [aoi_id]
        )

        # ── Polygon features (area basis) ─────────────────────────────────
        poly_rows = self.connection.execute(
            """
            SELECT
                CAST(f.id AS VARCHAR) AS feature_id,
                ST_AsWKB(f.geometry) AS feature_wkb,
                ST_AsWKB(ST_Intersection(f.geometry, aoi.geometry)) AS inter_wkb
            FROM curated_geo.feature f
            JOIN curated_geo.area_of_interest aoi ON aoi.id = ?
            WHERE ST_GeometryType(f.geometry) IN ('POLYGON', 'MULTIPOLYGON')
              AND ST_Intersects(f.geometry, aoi.geometry)
            """,
            [aoi_id],
        ).fetchall()

        insert_rows: list[tuple] = []
        for feature_id, feature_wkb, inter_wkb in poly_rows:
            if feature_wkb is None or inter_wkb is None:
                continue
            feature_geom = shapely.wkb.loads(bytes(feature_wkb))
            inter_geom = shapely.wkb.loads(bytes(inter_wkb))
            feature_area = abs(geod.geometry_area_perimeter(feature_geom)[0])
            inter_area = abs(geod.geometry_area_perimeter(inter_geom)[0])
            if inter_area <= 0 or feature_area <= 0:
                continue
            overlap_measure = min(inter_area, feature_area)
            overlap_ratio = min(overlap_measure / feature_area, 1.0)
            insert_rows.append(
                (
                    aoi_id,
                    feature_id,
                    "area",
                    overlap_measure,
                    feature_area,
                    overlap_ratio,
                )
            )

        # ── Line features (length basis) ──────────────────────────────────
        line_rows = self.connection.execute(
            """
            SELECT
                CAST(f.id AS VARCHAR) AS feature_id,
                ST_AsWKB(f.geometry) AS feature_wkb,
                ST_AsWKB(ST_Intersection(f.geometry, aoi.geometry)) AS inter_wkb
            FROM curated_geo.feature f
            JOIN curated_geo.area_of_interest aoi ON aoi.id = ?
            WHERE ST_GeometryType(f.geometry) IN ('LINESTRING', 'MULTILINESTRING')
              AND ST_Intersects(f.geometry, aoi.geometry)
            """,
            [aoi_id],
        ).fetchall()

        for feature_id, feature_wkb, inter_wkb in line_rows:
            if feature_wkb is None or inter_wkb is None:
                continue
            feature_geom = shapely.wkb.loads(bytes(feature_wkb))
            inter_geom = shapely.wkb.loads(bytes(inter_wkb))
            feature_len = geod.geometry_length(feature_geom)
            inter_len = geod.geometry_length(inter_geom)
            if inter_len <= 0 or feature_len <= 0:
                continue
            overlap_measure = min(inter_len, feature_len)
            overlap_ratio = min(overlap_measure / feature_len, 1.0)
            insert_rows.append(
                (
                    aoi_id,
                    feature_id,
                    "length",
                    overlap_measure,
                    feature_len,
                    overlap_ratio,
                )
            )

        # ── Point features (presence basis) ───────────────────────────────
        point_rows = self.connection.execute(
            """
            SELECT CAST(f.id AS VARCHAR) AS feature_id
            FROM curated_geo.feature f
            JOIN curated_geo.area_of_interest aoi ON aoi.id = ?
            WHERE ST_GeometryType(f.geometry) IN ('POINT', 'MULTIPOINT')
              AND ST_Within(f.geometry, aoi.geometry)
            """,
            [aoi_id],
        ).fetchall()

        for (feature_id,) in point_rows:
            insert_rows.append((aoi_id, feature_id, "presence", 1.0, 1.0, 1.0))

        if insert_rows:
            self.connection.executemany(
                """
                INSERT INTO curated_geo.aoi_feature
                  (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                insert_rows,
            )

        return len(insert_rows)

    def list_aois(self) -> list[dict]:
        # DuckDB's spatial extension exposes ST_XMin/ST_YMin/ST_XMax/ST_YMax;
        # if the extension isn't loaded the geometry column is just text and
        # we fall back to None bbox so the API still serves the row.
        #
        # Centroid: prefer the count-weighted block centroid (urban core)
        # over plain ST_PointOnSurface(aoi.geometry). For huge concave
        # AOIs like Mexicali (mostly desert), the polygon centroid can
        # land in farmland with no features visible. Block centroids
        # cluster in dense urban areas, so the average pulls toward
        # where the user expects to see data.
        rows = self.connection.execute(
            """
            SELECT CAST(aoi.id AS VARCHAR), CAST(aoi.project_id AS VARCHAR),
                   aoi.name, aoi.description,
                   ST_XMin(aoi.geometry) AS min_lon,
                   ST_YMin(aoi.geometry) AS min_lat,
                   ST_XMax(aoi.geometry) AS max_lon,
                   ST_YMax(aoi.geometry) AS max_lat,
                   COALESCE(
                     (SELECT ST_X(ST_Centroid(ST_Collect(list(ST_Centroid(f.geometry)))))
                      FROM curated_geo.aoi_feature af
                      JOIN curated_geo.feature f ON f.id = af.feature_id
                      WHERE af.aoi_id = aoi.id AND f.type_slug = 'block'),
                     ST_X(ST_PointOnSurface(aoi.geometry))
                   ) AS centroid_lon,
                   COALESCE(
                     (SELECT ST_Y(ST_Centroid(ST_Collect(list(ST_Centroid(f.geometry)))))
                      FROM curated_geo.aoi_feature af
                      JOIN curated_geo.feature f ON f.id = af.feature_id
                      WHERE af.aoi_id = aoi.id AND f.type_slug = 'block'),
                     ST_Y(ST_PointOnSurface(aoi.geometry))
                   ) AS centroid_lat,
                   -- Features bbox = envelope of all blocks loaded
                   -- inside this AOI. See postgres repo for the
                   -- rationale; mirrors the same SQL surface so the
                   -- API behaves identically across backends.
                   (SELECT ST_XMin(ST_Extent(f.geometry))
                    FROM curated_geo.aoi_feature af
                    JOIN curated_geo.feature f ON f.id = af.feature_id
                    WHERE af.aoi_id = aoi.id AND f.type_slug = 'block')
                     AS feat_min_lon,
                   (SELECT ST_YMin(ST_Extent(f.geometry))
                    FROM curated_geo.aoi_feature af
                    JOIN curated_geo.feature f ON f.id = af.feature_id
                    WHERE af.aoi_id = aoi.id AND f.type_slug = 'block')
                     AS feat_min_lat,
                   (SELECT ST_XMax(ST_Extent(f.geometry))
                    FROM curated_geo.aoi_feature af
                    JOIN curated_geo.feature f ON f.id = af.feature_id
                    WHERE af.aoi_id = aoi.id AND f.type_slug = 'block')
                     AS feat_max_lon,
                   (SELECT ST_YMax(ST_Extent(f.geometry))
                    FROM curated_geo.aoi_feature af
                    JOIN curated_geo.feature f ON f.id = af.feature_id
                    WHERE af.aoi_id = aoi.id AND f.type_slug = 'block')
                     AS feat_max_lat
            FROM curated_geo.area_of_interest aoi
            ORDER BY aoi.name
            """
        ).fetchall()
        return [
            {
                "aoi_id": aid,
                "project_id": pid,
                "name": name,
                "description": descr,
                "bbox": [min_lon, min_lat, max_lon, max_lat]
                if min_lon is not None
                else None,
                "centroid": [centroid_lon, centroid_lat]
                if centroid_lon is not None
                else None,
                "features_bbox": (
                    [feat_min_lon, feat_min_lat, feat_max_lon, feat_max_lat]
                    if feat_min_lon is not None
                    else None
                ),
            }
            for aid, pid, name, descr, min_lon, min_lat, max_lon, max_lat, centroid_lon, centroid_lat, feat_min_lon, feat_min_lat, feat_max_lon, feat_max_lat in rows
        ]

    def get_aoi(self, aoi_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR), CAST(project_id AS VARCHAR), name, description
            FROM curated_geo.area_of_interest
            WHERE CAST(id AS VARCHAR) = ?
            """,
            [aoi_id],
        ).fetchone()
        if row is None:
            return None
        aid, pid, name, descr = row
        return {"aoi_id": aid, "project_id": pid, "name": name, "description": descr}

    def upsert_project_by_name(self, name: str, description: str | None = None) -> str:
        existing = self.connection.execute(
            "SELECT CAST(id AS VARCHAR) FROM app_core.project WHERE name = ? LIMIT 1",
            [name],
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        # DuckDB: no RETURNING; generate UUID client-side.
        project_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO app_core.project (id, name, description) VALUES (?, ?, ?)",
            [project_id, name, description],
        )
        return project_id


class DuckDBOverlapRepository:
    """Compute feature-to-feature spatial overlaps using geodesic measurements.

    Uses pyproj.Geod + shapely.wkb for WGS84 ellipsoid-accurate measurements,
    matching the PostgreSQL backend's ::geography behaviour.
    """

    def __init__(self, connection):
        self.connection = connection

    def compute_feature_overlaps(
        self,
        source_type_id: str,
        target_type_id: str,
        *,
        aoi_id: str | None = None,
    ) -> int:
        import shapely.wkb
        from pyproj import Geod

        geod = Geod(ellps="WGS84")

        # Validate both type IDs up front so callers get a clear error rather than
        # a silent empty result.
        src_row = self.connection.execute(
            "SELECT geometry_family FROM curated_geo.feature_type_catalog WHERE CAST(id AS VARCHAR) = ?",
            [source_type_id],
        ).fetchone()
        if src_row is None:
            raise ValueError(f"Unknown source_type_id: {source_type_id!r}")
        source_family: str = src_row[0]

        tgt_row = self.connection.execute(
            "SELECT id FROM curated_geo.feature_type_catalog WHERE CAST(id AS VARCHAR) = ?",
            [target_type_id],
        ).fetchone()
        if tgt_row is None:
            raise ValueError(f"Unknown target_type_id: {target_type_id!r}")

        if aoi_id is not None:
            aoi_row = self.connection.execute(
                "SELECT 1 FROM curated_geo.area_of_interest WHERE CAST(id AS VARCHAR) = ?",
                [aoi_id],
            ).fetchone()
            if aoi_row is None:
                raise ValueError(f"Unknown aoi_id: {aoi_id!r}")

        aoi_join_clause = (
            " JOIN curated_geo.area_of_interest aoi"
            "   ON CAST(aoi.id AS VARCHAR) = ?"
            "   AND ST_Intersects(sf.geometry, aoi.geometry)"
            if aoi_id is not None
            else ""
        )
        aoi_params = [aoi_id] if aoi_id is not None else []

        _UPSERT_SQL = """
            INSERT INTO curated_geo.feature_overlap
              (source_feature_id, target_feature_id, overlap_basis,
               overlap_measure, feature_measure, overlap_ratio)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_feature_id, target_feature_id) DO UPDATE
              SET overlap_basis    = EXCLUDED.overlap_basis,
                  overlap_measure  = EXCLUDED.overlap_measure,
                  feature_measure  = EXCLUDED.feature_measure,
                  overlap_ratio    = EXCLUDED.overlap_ratio,
                  computed_at      = now()
        """
        _CHUNK = 500  # rows fetched per round-trip; bounds Python memory usage

        insert_rows: list[tuple] = []

        if source_family == "polygon":
            result = self.connection.execute(
                f"""
                SELECT
                    CAST(sf.id AS VARCHAR),
                    CAST(tf.id AS VARCHAR),
                    ST_AsWKB(sf.geometry),
                    ST_AsWKB(ST_Intersection(sf.geometry, tf.geometry))
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Intersects(sf.geometry, tf.geometry){aoi_join_clause}
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, *aoi_params, source_type_id],
            )
            while True:
                batch = result.fetchmany(_CHUNK)
                if not batch:
                    break
                for src_id, tgt_id, src_wkb, inter_wkb in batch:
                    if src_wkb is None or inter_wkb is None:
                        continue
                    src_geom = shapely.wkb.loads(bytes(src_wkb))
                    inter_geom = shapely.wkb.loads(bytes(inter_wkb))
                    feature_area = abs(geod.geometry_area_perimeter(src_geom)[0])
                    inter_area = abs(geod.geometry_area_perimeter(inter_geom)[0])
                    if inter_area <= 0 or feature_area <= 0:
                        continue
                    overlap_measure = min(inter_area, feature_area)
                    overlap_ratio = min(overlap_measure / feature_area, 1.0)
                    insert_rows.append(
                        (
                            src_id,
                            tgt_id,
                            "area",
                            overlap_measure,
                            feature_area,
                            overlap_ratio,
                        )
                    )

        elif source_family == "line":
            result = self.connection.execute(
                f"""
                SELECT
                    CAST(sf.id AS VARCHAR),
                    CAST(tf.id AS VARCHAR),
                    ST_AsWKB(sf.geometry),
                    ST_AsWKB(ST_Intersection(sf.geometry, tf.geometry))
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Intersects(sf.geometry, tf.geometry){aoi_join_clause}
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, *aoi_params, source_type_id],
            )
            while True:
                batch = result.fetchmany(_CHUNK)
                if not batch:
                    break
                for src_id, tgt_id, src_wkb, inter_wkb in batch:
                    if src_wkb is None or inter_wkb is None:
                        continue
                    src_geom = shapely.wkb.loads(bytes(src_wkb))
                    inter_geom = shapely.wkb.loads(bytes(inter_wkb))
                    feature_len = geod.geometry_length(src_geom)
                    inter_len = geod.geometry_length(inter_geom)
                    if inter_len <= 0 or feature_len <= 0:
                        continue
                    overlap_measure = min(inter_len, feature_len)
                    overlap_ratio = min(overlap_measure / feature_len, 1.0)
                    insert_rows.append(
                        (
                            src_id,
                            tgt_id,
                            "length",
                            overlap_measure,
                            feature_len,
                            overlap_ratio,
                        )
                    )

        else:  # point or geometry — presence basis
            pairs = self.connection.execute(
                f"""
                SELECT CAST(sf.id AS VARCHAR), CAST(tf.id AS VARCHAR)
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Within(sf.geometry, tf.geometry){aoi_join_clause}
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, *aoi_params, source_type_id],
            ).fetchall()
            insert_rows = [
                (src_id, tgt_id, "presence", 1.0, 1.0, 1.0) for src_id, tgt_id in pairs
            ]

        if insert_rows:
            self.connection.executemany(_UPSERT_SQL, insert_rows)

        return len(insert_rows)

    def rollup_child_rows(
        self,
        *,
        indicator_slug: str,
        source_feature_type: str,
        target_feature_type: str,
        scenario_id: str | None = None,
    ) -> list[tuple[str, float | None, float, str]]:
        scenario_clause, scenario_params = _scenario_filter(
            "CAST(ir.scenario_id AS VARCHAR)", scenario_id, "?"
        )
        params: list = [
            source_feature_type,
            target_feature_type,
            indicator_slug,
            *scenario_params,
        ]
        rows = self.connection.execute(
            f"""
            SELECT CAST(fo.target_feature_id AS VARCHAR),
                   ir.value_numeric,
                   fo.overlap_measure,
                   ir.quality_flag
            FROM curated_geo.feature_overlap fo
            JOIN curated_geo.feature src
              ON src.id = fo.source_feature_id AND src.type_slug = ?
            JOIN curated_geo.feature tgt
              ON tgt.id = fo.target_feature_id AND tgt.type_slug = ?
            LEFT JOIN curated_geo.indicator_result ir
              ON ir.feature_id = fo.source_feature_id
             AND ir.indicator_slug = ?
             AND ir.status = 'current'
             AND {scenario_clause}
            """,
            params,
        ).fetchall()
        return [
            (
                str(target_id),
                float(value) if value is not None else None,
                float(weight),
                quality or "missing",
            )
            for target_id, value, weight, quality in rows
        ]

    def rollup_observation_child_rows(
        self,
        *,
        input_attribute_key_prefix: str,
        source_feature_type: str,
        target_feature_type: str,
        aoi_id: str | None = None,
    ) -> list[tuple[str, str, float, float]]:
        if not input_attribute_key_prefix:
            raise ValueError("input_attribute_key_prefix is required")
        aoi_clause = ""
        aoi_params: list = []
        if aoi_id is not None:
            aoi_clause = (
                " AND fo.source_feature_id IN "
                "(SELECT feature_id FROM curated_geo.aoi_feature WHERE aoi_id = ?)"
            )
            aoi_params.append(aoi_id)
        rows = self.connection.execute(
            f"""
            SELECT CAST(fo.target_feature_id AS VARCHAR),
                   obs.attribute_key,
                   obs.value_numeric,
                   fo.overlap_measure
            FROM curated_geo.feature_overlap fo
            JOIN curated_geo.feature src
              ON src.id = fo.source_feature_id AND src.type_slug = ?
            JOIN curated_geo.feature tgt
              ON tgt.id = fo.target_feature_id AND tgt.type_slug = ?
            JOIN curated_geo.observation obs
              ON obs.feature_id = fo.source_feature_id
             AND obs.is_current
             AND obs.value_numeric IS NOT NULL
             AND (obs.attribute_key = ?
                  OR starts_with(obs.attribute_key, ? || '_'))
            WHERE TRUE{aoi_clause}
            """,
            [
                source_feature_type,
                target_feature_type,
                input_attribute_key_prefix,
                input_attribute_key_prefix,
                *aoi_params,
            ],
        ).fetchall()
        return [
            (str(target_id), str(attr_key), float(value), float(weight))
            for target_id, attr_key, value, weight in rows
        ]


class DuckDBOverlapTypePairRepository:
    """Declared (source_type, target_type) pairs on DuckDB."""

    def __init__(self, connection):
        self.connection = connection

    def upsert_pair(self, source_type_id: str, target_type_id: str) -> str:
        existing = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR)
            FROM curated_geo.feature_overlap_type_pair
            WHERE source_type_id = ? AND target_type_id = ?
            """,
            [source_type_id, target_type_id],
        ).fetchone()
        if existing is not None:
            return existing[0]

        pair_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.feature_overlap_type_pair
              (id, source_type_id, target_type_id)
            VALUES (?, ?, ?)
            """,
            [pair_id, source_type_id, target_type_id],
        )
        return pair_id

    def list_pairs(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT
                CAST(p.id AS VARCHAR)             AS pair_id,
                CAST(p.source_type_id AS VARCHAR) AS source_type_id,
                sc.context || ':' || sc.type_slug AS source,
                CAST(p.target_type_id AS VARCHAR) AS target_type_id,
                tc.context || ':' || tc.type_slug AS target
            FROM curated_geo.feature_overlap_type_pair p
            JOIN curated_geo.feature_type_catalog sc ON sc.id = p.source_type_id
            JOIN curated_geo.feature_type_catalog tc ON tc.id = p.target_type_id
            ORDER BY sc.context, sc.type_slug, tc.context, tc.type_slug
            """
        ).fetchall()
        cols = ("pair_id", "source_type_id", "source", "target_type_id", "target")
        return [dict(zip(cols, row)) for row in rows]


class DuckDBServingReader:
    def __init__(self, connection):
        self.connection = connection

    def get_features(
        self,
        *,
        type_slug: str | None = None,
        role: str | None = None,
        aoi_id: str | None = None,
        limit: int = 500,
        after_cursor: str | None = None,
    ) -> dict:
        from .._utils import decode_cursor, encode_cursor

        conditions: list[str] = []
        params: list = []

        if type_slug:
            conditions.append("f.type_slug = ?")
            params.append(type_slug)
        if role:
            conditions.append("f.role = ?")
            params.append(role)
        if after_cursor:
            cursor_ts, cursor_id = decode_cursor(after_cursor)
            # CAST(f.id AS VARCHAR) produces lowercase canonical UUID strings
            # (xxxxxxxx-xxxx-…).  String comparison on that form is consistent
            # within DuckDB, matching the ORDER BY CAST(f.id AS VARCHAR) below.
            # Postgres uses native UUID comparison (f.id > %s::uuid) — same
            # stable tiebreaker, different type; cursors are not cross-backend.
            conditions.append(
                "(f.created_at > CAST(? AS TIMESTAMPTZ)"
                " OR (f.created_at = CAST(? AS TIMESTAMPTZ) AND CAST(f.id AS VARCHAR) > ?))"
            )
            params.extend([cursor_ts, cursor_ts, cursor_id])

        fetch_limit = limit + 1

        if aoi_id:
            extra_conditions = (
                (" AND " + " AND ".join(conditions)) if conditions else ""
            )
            result = self.connection.execute(
                f"""
                SELECT
                    CAST(f.id AS VARCHAR) AS id,
                    f.type_slug,
                    f.role,
                    f.name,
                    CAST(f.attributes AS VARCHAR) AS attributes,
                    f.overlap_ratio,
                    f.created_at
                FROM serving_geo.feature_aoi_v f
                WHERE CAST(f.aoi_id AS VARCHAR) = ?{extra_conditions}
                ORDER BY f.created_at, CAST(f.id AS VARCHAR)
                LIMIT ?
                """,
                [aoi_id, *params, fetch_limit],
            )
        else:
            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            result = self.connection.execute(
                f"""
                SELECT
                    CAST(f.id AS VARCHAR) AS id,
                    f.type_slug,
                    f.role,
                    f.name,
                    CAST(f.attributes AS VARCHAR) AS attributes,
                    f.created_at
                FROM serving_geo.feature_v f
                {where_clause}
                ORDER BY f.created_at, CAST(f.id AS VARCHAR)
                LIMIT ?
                """,
                [*params, fetch_limit],
            )

        rows = _rows_as_dicts(result)
        # Normalise attributes to dict | None here so callers see the same
        # contract as the Postgres RealDictCursor-backed reader. Malformed
        # JSON degrades to None rather than raising — the serving view is
        # best-effort for inspection, not a place to fail a request.
        for row in rows:
            value = row.get("attributes")
            if isinstance(value, str):
                try:
                    row["attributes"] = json.loads(value)
                except (TypeError, ValueError):
                    row["attributes"] = None

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])

        for row in rows:
            row.pop("created_at", None)

        return {"features": rows, "next_cursor": next_cursor}

    def count_features(self, *, type_slug: str | None = None) -> int:
        if type_slug is None:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM serving_geo.feature_v"
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM serving_geo.feature_v WHERE type_slug = ?",
                [type_slug],
            ).fetchone()
        return int(row[0])

    def count_features_within_polygon(
        self,
        *,
        aoi_id: str,
        polygon_geojson: str,
        type_slugs: list[str],
    ) -> dict[str, int]:
        # ST_Intersects on a polygon vs. a GiST-indexed feature column is
        # PostGIS-specific; DuckDB's ``spatial`` extension supports the
        # primitive but the serving views don't expose a spatial-indexed
        # column today. The API only ever calls this method via the
        # postgres backend (api/deps.py), so a clean error here keeps the
        # contract honest without forcing a parallel DuckDB
        # implementation.
        raise NotImplementedError(
            "count_features_within_polygon requires PostGIS — DuckDB backend "
            "does not yet support spatial within-polygon queries.",
        )


# Columns that are stored as JSON in the DB and returned as strings by DuckDB.
# Only these are JSON-decoded; other string values are left untouched.
_EXPLORE_JSON_COLUMNS: frozenset[str] = frozenset(
    {
        "details",
        "attributes",
        "band_description",
        "schema_definition",
        "lineage_ref",
    }
)


class DuckDBExploreReader:
    def __init__(self, connection):
        self.connection = connection

    def _read_query(self, sql: str) -> list[dict]:
        result = self.connection.execute(sql)
        rows = _rows_as_dicts(result)
        for row in rows:
            for key, value in list(row.items()):
                if key in _EXPLORE_JSON_COLUMNS and isinstance(value, str):
                    try:
                        row[key] = json.loads(value)
                    except Exception:
                        pass
        return rows

    def list_layers(self) -> list[dict]:
        return self._read_query(
            """
            SELECT *
            FROM explore.layer_catalog_v
            ORDER BY
              CASE layer_kind
                WHEN 'canonical' THEN 1
                WHEN 'serving' THEN 2
                ELSE 3
              END,
              schema_name,
              object_name
            """
        )

    def list_sources(self) -> list[dict]:
        return self._read_query(
            """
            SELECT *
            FROM explore.source_runs_v
            ORDER BY source_name, version, started_at NULLS LAST, ingestion_run_id
            """
        )

    def list_feature_stats(self) -> list[dict]:
        return self._read_query(
            """
            SELECT *
            FROM explore.feature_type_stats_v
            ORDER BY row_count DESC, type_slug, role, source_id, dataset_version
            """
        )

    def list_serving_stats(self) -> list[dict]:
        return self._read_query(
            """
            SELECT *
            FROM explore.serving_stats_v
            ORDER BY stat_scope, row_count DESC, type_slug, role
            """
        )


class DuckDBTileSnapshotRepository:
    def __init__(self, connection):
        self.connection = connection

    def register_snapshot(
        self,
        *,
        layer_id: str,
        artifact_path: str,
        artifact_uri: str | None,
        snapshot_version: str,
        source_relation: str,
        source_lineage_ref: dict | None = None,
        status: str = "ready",
    ) -> str:
        snapshot_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.tile_snapshot_registry
              (id, layer_id, artifact_path, artifact_uri, snapshot_version, source_relation, source_lineage_ref, status)
            VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?)
            """,
            [
                snapshot_id,
                layer_id,
                artifact_path,
                artifact_uri,
                snapshot_version,
                source_relation,
                json.dumps(source_lineage_ref or {}),
                status,
            ],
        )
        return snapshot_id

    def get_latest_snapshot(self, *, layer_id: str) -> dict | None:
        rows = _rows_as_dicts(
            self.connection.execute(
                """
                SELECT
                  id,
                  layer_id,
                  artifact_path,
                  artifact_uri,
                  snapshot_version,
                  source_relation,
                  source_lineage_ref,
                  status,
                  created_at
                FROM curated_geo.tile_snapshot_registry
                WHERE layer_id = ?
                ORDER BY created_at DESC, snapshot_version DESC
                LIMIT 1
                """,
                [layer_id],
            )
        )
        return rows[0] if rows else None

    def list_snapshots(self, *, layer_id: str | None = None) -> list[dict]:
        if layer_id:
            return _rows_as_dicts(
                self.connection.execute(
                    """
                    SELECT
                      id,
                      layer_id,
                      artifact_path,
                      artifact_uri,
                      snapshot_version,
                      source_relation,
                      source_lineage_ref,
                      status,
                      created_at
                    FROM curated_geo.tile_snapshot_registry
                    WHERE layer_id = ?
                    ORDER BY created_at DESC, snapshot_version DESC
                    """,
                    [layer_id],
                )
            )
        return _rows_as_dicts(
            self.connection.execute(
                """
                SELECT
                  id,
                  layer_id,
                  artifact_path,
                  artifact_uri,
                  snapshot_version,
                  source_relation,
                  source_lineage_ref,
                  status,
                  created_at
                FROM curated_geo.tile_snapshot_registry
                ORDER BY created_at DESC, snapshot_version DESC
                """
            )
        )


_AOI_SUMMARY_COLS = (
    "id",
    "aoi_id",
    "scenario_id",
    "indicator_id",
    "value",
    "aggregation_note",
    "status",
    "computed_at",
)


class DuckDBAOISummaryRepository:
    def __init__(self, connection):
        self.connection = connection

    def upsert_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        value: float | None,
        scenario_id: str | None = None,
        aggregation_note: str | None = None,
        status: str = "current",
    ) -> str:
        # Manual upsert: DuckDB's partial unique indexes do not support
        # ON CONFLICT with a predicate, so we do a read-modify-write here.
        scenario_clause, scenario_params = _scenario_filter(
            "scenario_id", scenario_id, "?"
        )
        existing = self.connection.execute(
            f"""
            SELECT CAST(id AS VARCHAR)
            FROM curated_geo.aoi_indicator_summary
            WHERE aoi_id = ? AND {scenario_clause} AND indicator_id = ?
            """,
            [aoi_id, *scenario_params, indicator_id],
        ).fetchone()

        if existing is not None:
            summary_id = existing[0]
            self.connection.execute(
                """
                UPDATE curated_geo.aoi_indicator_summary
                SET value = ?,
                    aggregation_note = ?,
                    status = ?,
                    computed_at = now()
                WHERE CAST(id AS VARCHAR) = ?
                """,
                [value, aggregation_note, status, summary_id],
            )
            return summary_id

        summary_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.aoi_indicator_summary
              (id, aoi_id, scenario_id, indicator_id, value, aggregation_note, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                summary_id,
                aoi_id,
                scenario_id,
                indicator_id,
                value,
                aggregation_note,
                status,
            ],
        )
        return summary_id

    def get_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        scenario_id: str | None = None,
    ) -> dict | None:
        scenario_clause, scenario_params = _scenario_filter(
            "scenario_id", scenario_id, "?"
        )
        row = self.connection.execute(
            f"""
            SELECT CAST(id AS VARCHAR), CAST(aoi_id AS VARCHAR),
                   CAST(scenario_id AS VARCHAR), CAST(indicator_id AS VARCHAR),
                   value, aggregation_note, status, computed_at
            FROM curated_geo.aoi_indicator_summary
            WHERE aoi_id = ? AND {scenario_clause} AND indicator_id = ?
            """,
            [aoi_id, *scenario_params, indicator_id],
        ).fetchone()
        return dict(zip(_AOI_SUMMARY_COLS, row)) if row else None

    def list_summaries(
        self,
        *,
        aoi_id: str,
        scenario_id: str | None = None,
        only_current: bool = True,
    ) -> list[dict]:
        conditions = ["aoi_id = ?"]
        params: list = [aoi_id]
        clause, extra = _scenario_filter("scenario_id", scenario_id, "?")
        conditions.append(clause)
        params.extend(extra)
        if only_current:
            conditions.append("status = 'current'")

        where_clause = " AND ".join(conditions)
        rows = self.connection.execute(
            f"""
            SELECT CAST(id AS VARCHAR), CAST(aoi_id AS VARCHAR),
                   CAST(scenario_id AS VARCHAR), CAST(indicator_id AS VARCHAR),
                   value, aggregation_note, status, computed_at
            FROM curated_geo.aoi_indicator_summary
            WHERE {where_clause}
            ORDER BY computed_at DESC
            """,
            params,
        ).fetchall()
        return [dict(zip(_AOI_SUMMARY_COLS, row)) for row in rows]

    def fetch_aoi_children(
        self,
        *,
        aoi_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> list[tuple[float | None, float, str]]:
        # See Protocol docstring for why weight is overlap_measure.
        scenario_clause, scenario_params = _scenario_filter(
            "CAST(ir.scenario_id AS VARCHAR)", scenario_id, "?"
        )
        params: list = [indicator_slug, *scenario_params, aoi_id]
        rows = self.connection.execute(
            f"""
            SELECT ir.value_numeric,
                   af.overlap_measure,
                   ir.quality_flag
            FROM curated_geo.aoi_feature af
            LEFT JOIN curated_geo.indicator_result ir
              ON ir.feature_id = af.feature_id
             AND ir.indicator_slug = ?
             AND ir.status = 'current'
             AND {scenario_clause}
            WHERE CAST(af.aoi_id AS VARCHAR) = ?
            """,
            params,
        ).fetchall()
        return [
            (
                float(value) if value is not None else None,
                float(weight),
                quality or "missing",
            )
            for value, weight, quality in rows
        ]


# ─── Job run ────────────────────────────────────────────────────────────────


class DuckDBJobRunRepository:
    def __init__(self, connection):
        self.connection = connection

    def start_job(
        self,
        *,
        model_family: str,
        code_version: str,
        scenario_id: str | None = None,
        status: str = "queued",
    ) -> str:
        job_run_id = str(uuid4())
        started_at = "now()" if status == "running" else "NULL"
        self.connection.execute(
            f"""
            INSERT INTO jobs_runtime.job_run
              (id, scenario_id, model_family, code_version, status, started_at)
            VALUES (?, ?, ?, ?, ?, {started_at})
            """,
            [job_run_id, scenario_id, model_family, code_version, status],
        )
        return job_run_id

    def complete_job(
        self,
        job_run_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE jobs_runtime.job_run
            SET status = ?,
                completed_at = now(),
                error_message = ?,
                started_at = COALESCE(started_at, now())
            WHERE CAST(id AS VARCHAR) = ?
            """,
            [status, error_message, job_run_id],
        )

    def get_job(self, job_run_id: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR), CAST(scenario_id AS VARCHAR),
                   model_family, status, code_version,
                   queued_at, started_at, completed_at, error_message
            FROM jobs_runtime.job_run WHERE CAST(id AS VARCHAR) = ?
            """,
            [job_run_id],
        ).fetchone()
        if row is None:
            return None
        cols = (
            "id",
            "scenario_id",
            "model_family",
            "status",
            "code_version",
            "queued_at",
            "started_at",
            "completed_at",
            "error_message",
        )
        return dict(zip(cols, row))


# ─── Indicator catalog ──────────────────────────────────────────────────────


class DuckDBIndicatorCatalogRepository:
    def __init__(self, connection):
        self.connection = connection

    def upsert_indicator(
        self,
        *,
        slug: str,
        name: str,
        computation_kind: str,
        value_type: str,
        aggregation_method: str,
        applies_to: list[str],
        description: str | None = None,
        model_family: str | None = None,
        unit: str | None = None,
        valid_min: float | None = None,
        valid_max: float | None = None,
    ) -> str:
        existing = self.connection.execute(
            "SELECT CAST(id AS VARCHAR) FROM curated_geo.indicator_catalog WHERE slug = ?",
            [slug],
        ).fetchone()
        if existing is not None:
            indicator_id = existing[0]
            self.connection.execute(
                """
                UPDATE curated_geo.indicator_catalog
                SET name = ?, description = ?, model_family = ?,
                    computation_kind = ?, value_type = ?, unit = ?,
                    valid_min = ?, valid_max = ?, aggregation_method = ?,
                    applies_to = ?
                WHERE slug = ?
                """,
                [
                    name,
                    description,
                    model_family,
                    computation_kind,
                    value_type,
                    unit,
                    valid_min,
                    valid_max,
                    aggregation_method,
                    applies_to,
                    slug,
                ],
            )
            return indicator_id

        indicator_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.indicator_catalog
              (id, slug, name, description, model_family, computation_kind, value_type,
               unit, valid_min, valid_max, aggregation_method, applies_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                indicator_id,
                slug,
                name,
                description,
                model_family,
                computation_kind,
                value_type,
                unit,
                valid_min,
                valid_max,
                aggregation_method,
                applies_to,
            ],
        )
        return indicator_id

    def get_indicator(self, slug: str) -> dict | None:
        row = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR), slug, name, description, model_family,
                   computation_kind, value_type, unit, valid_min, valid_max,
                   aggregation_method, applies_to
            FROM curated_geo.indicator_catalog WHERE slug = ?
            """,
            [slug],
        ).fetchone()
        if row is None:
            return None
        cols = (
            "id",
            "slug",
            "name",
            "description",
            "model_family",
            "computation_kind",
            "value_type",
            "unit",
            "valid_min",
            "valid_max",
            "aggregation_method",
            "applies_to",
        )
        return dict(zip(cols, row))

    def list_indicators(self) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR), slug, name, description, model_family,
                   computation_kind, value_type, unit, valid_min, valid_max,
                   aggregation_method, applies_to
            FROM curated_geo.indicator_catalog ORDER BY slug
            """
        ).fetchall()
        cols = (
            "id",
            "slug",
            "name",
            "description",
            "model_family",
            "computation_kind",
            "value_type",
            "unit",
            "valid_min",
            "valid_max",
            "aggregation_method",
            "applies_to",
        )
        return [dict(zip(cols, row)) for row in rows]

    def set_dependencies(
        self,
        *,
        indicator_slug: str,
        depends_on: list[tuple[str, float | None]],
    ) -> None:
        parent = self.connection.execute(
            "SELECT CAST(id AS VARCHAR) FROM curated_geo.indicator_catalog WHERE slug = ?",
            [indicator_slug],
        ).fetchone()
        if parent is None:
            raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
        parent_id = parent[0]

        self.connection.execute(
            "DELETE FROM curated_geo.indicator_dependency WHERE CAST(indicator_id AS VARCHAR) = ?",
            [parent_id],
        )
        for dep_slug, weight in depends_on:
            child = self.connection.execute(
                "SELECT CAST(id AS VARCHAR) FROM curated_geo.indicator_catalog WHERE slug = ?",
                [dep_slug],
            ).fetchone()
            if child is None:
                raise ValueError(f"Unknown dependency slug: {dep_slug!r}")
            self.connection.execute(
                """
                INSERT INTO curated_geo.indicator_dependency
                  (indicator_id, depends_on_id, weight)
                VALUES (?, ?, ?)
                """,
                [parent_id, child[0], weight],
            )


# ─── Indicator result ──────────────────────────────────────────────────────


_INDICATOR_VALUE_COLS = ("value_numeric", "value_text", "value_json")


class DuckDBIndicatorResultRepository:
    """Enforces the Postgres before-write-trigger semantics in Python.

    Uniqueness of (feature_id, scenario_id, indicator_id, status='current')
    is maintained by marking existing current rows stale before every insert
    — same ordering as the Postgres repository.
    """

    def __init__(self, connection):
        self.connection = connection

    def _lookup_feature_type(self, feature_id: str) -> str:
        row = self.connection.execute(
            "SELECT type_slug FROM curated_geo.feature WHERE CAST(id AS VARCHAR) = ?",
            [feature_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"indicator_result.feature_id {feature_id!r} not found")
        return row[0]

    def _lookup_catalog(
        self, indicator_slug: str
    ) -> tuple[str, list[str], float | None, float | None]:
        row = self.connection.execute(
            """
            SELECT CAST(id AS VARCHAR), applies_to, valid_min, valid_max
            FROM curated_geo.indicator_catalog WHERE slug = ?
            """,
            [indicator_slug],
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
        return row[0], list(row[1] or []), row[2], row[3]

    def _lookup_run(self, job_run_id: str) -> tuple[str | None, str]:
        row = self.connection.execute(
            """
            SELECT CAST(scenario_id AS VARCHAR), code_version
            FROM jobs_runtime.job_run WHERE CAST(id AS VARCHAR) = ?
            """,
            [job_run_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"indicator_result.job_run_id {job_run_id!r} not found")
        return row[0], row[1]

    def _mark_stale(
        self,
        *,
        feature_id: str,
        indicator_id: str,
        scenario_id: str | None,
    ) -> None:
        scenario_clause, scenario_params = _scenario_filter(
            "CAST(scenario_id AS VARCHAR)", scenario_id, "?"
        )
        self.connection.execute(
            f"""
            UPDATE curated_geo.indicator_result
            SET status = 'stale', updated_at = now()
            WHERE CAST(feature_id AS VARCHAR) = ?
              AND {scenario_clause}
              AND CAST(indicator_id AS VARCHAR) = ?
              AND status = 'current'
            """,
            [feature_id, *scenario_params, indicator_id],
        )

    def write_result(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        job_run_id: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
        value_json=None,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
        confidence: float | None = None,
        missing_reason: str | None = None,
        model_version: str | None = None,
    ) -> str:
        feature_type = self._lookup_feature_type(feature_id)
        indicator_id, applies_to, valid_min, valid_max = self._lookup_catalog(
            indicator_slug
        )
        run_scenario, run_code_version = self._lookup_run(job_run_id)
        resolved_model_version = model_version or run_code_version
        validate_result_rows(
            [
                {
                    "value_numeric": value_numeric,
                    "value_text": value_text,
                    "value_json": value_json,
                    "quality_flag": quality_flag,
                    "status": "current",
                    "missing_reason": missing_reason,
                    "feature_type": feature_type,
                    "scenario_id": scenario_id,
                }
            ],
            applies_to=applies_to,
            valid_min=valid_min,
            valid_max=valid_max,
            run_scenario=run_scenario,
        )
        self._mark_stale(
            feature_id=feature_id,
            indicator_id=indicator_id,
            scenario_id=scenario_id,
        )

        result_id = str(uuid4())
        value_json_str = json.dumps(value_json) if value_json is not None else None
        self.connection.execute(
            """
            INSERT INTO curated_geo.indicator_result
              (id, feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
               job_run_id, model_version, value_numeric, value_text, value_json,
               quality_flag, confidence, missing_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?, ?, 'current')
            """,
            [
                result_id,
                feature_id,
                feature_type,
                scenario_id,
                indicator_id,
                indicator_slug,
                job_run_id,
                resolved_model_version,
                value_numeric,
                value_text,
                value_json_str,
                quality_flag,
                confidence,
                missing_reason,
            ],
        )
        return result_id

    def bulk_write_numeric_results(
        self,
        rows: list[tuple[str, float]],
        *,
        indicator_slug: str,
        job_run_id: str,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
    ) -> int:
        if not rows:
            return 0
        indicator_id, applies_to, valid_min, valid_max = self._lookup_catalog(
            indicator_slug
        )
        run_scenario, run_code_version = self._lookup_run(job_run_id)
        if scenario_id != run_scenario:
            raise ValueError(
                f"scenario_id {scenario_id!r} must match job_run.scenario_id {run_scenario!r}"
            )
        feature_ids = [fid for fid, _ in rows]
        feature_types = {
            str(fid): ftype
            for fid, ftype in self.connection.execute(
                """
                SELECT CAST(id AS VARCHAR), type_slug
                FROM curated_geo.feature
                WHERE CAST(id AS VARCHAR) = ANY(?)
                """,
                [feature_ids],
            ).fetchall()
        }
        validation_rows: list[dict] = []
        for fid, value in rows:
            ftype = feature_types.get(str(fid))
            if ftype is None:
                raise ValueError(f"feature_id {fid!r} not found in curated_geo.feature")
            validation_rows.append(
                {
                    "value_numeric": value,
                    "value_text": None,
                    "value_json": None,
                    "quality_flag": quality_flag,
                    "status": "current",
                    "missing_reason": None,
                    "feature_type": ftype,
                    "scenario_id": scenario_id,
                }
            )
        validate_result_rows(
            validation_rows,
            applies_to=applies_to,
            valid_min=valid_min,
            valid_max=valid_max,
            run_scenario=run_scenario,
        )

        insert_batch: list[tuple] = []
        for (fid, value), vrow in zip(rows, validation_rows):
            ftype = vrow["feature_type"]
            self._mark_stale(
                feature_id=fid,
                indicator_id=indicator_id,
                scenario_id=scenario_id,
            )
            insert_batch.append(
                (
                    str(uuid4()),
                    fid,
                    ftype,
                    scenario_id,
                    indicator_id,
                    indicator_slug,
                    job_run_id,
                    run_code_version,
                    value,
                    quality_flag,
                )
            )

        self.connection.executemany(
            """
            INSERT INTO curated_geo.indicator_result
              (id, feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
               job_run_id, model_version, value_numeric, quality_flag, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current')
            """,
            insert_batch,
        )
        return len(insert_batch)

    def get_current(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> dict | None:
        scenario_clause, scenario_params = _scenario_filter(
            "scenario_id", scenario_id, "?"
        )
        row = self.connection.execute(
            f"""
            SELECT * FROM serving_geo.current_indicator_v
            WHERE feature_id = ? AND {scenario_clause} AND indicator_slug = ?
            """,
            [feature_id, *scenario_params, indicator_slug],
        ).fetchone()
        if row is None:
            return None
        return _row_as_dict_current_indicator(row)

    def list_current_for_indicator(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        scenario_clause, scenario_params = _scenario_filter(
            "scenario_id", scenario_id, "?"
        )
        limit_sql = "" if limit is None else "LIMIT ?"
        limit_params: list = [] if limit is None else [limit]
        rows = self.connection.execute(
            f"""
            SELECT * FROM serving_geo.current_indicator_v
            WHERE indicator_slug = ? AND {scenario_clause}
            {limit_sql}
            """,
            [indicator_slug, *scenario_params, *limit_params],
        ).fetchall()
        return [_row_as_dict_current_indicator(row) for row in rows]

    def list_current_with_geometry(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        limit: int = 100_000,
    ) -> list[dict]:
        """DuckDB mirror of PostgresIndicatorResultRepository.list_current_with_geometry.

        Emits EPSG:4326 WKB as bytes so the API download path can treat
        both backends identically. Ordering + limit behaviour match.
        """
        clauses = ["ir.indicator_slug = ?", "ir.status = 'current'"]
        params: list = [indicator_slug]
        clause, extra = _scenario_filter(
            "CAST(ir.scenario_id AS VARCHAR)", scenario_id, "?"
        )
        clauses.append(clause)
        params.extend(extra)
        if feature_type is not None:
            clauses.append("ir.feature_type = ?")
            params.append(feature_type)
        params.append(limit)

        rows = self.connection.execute(
            f"""
            SELECT CAST(ir.feature_id AS VARCHAR) AS feature_id,
                   ir.feature_type                AS type_slug,
                   ST_AsWKB(f.geometry)           AS geometry_wkb,
                   ir.value_numeric               AS value_numeric,
                   ir.quality_flag                AS quality_flag,
                   ir.computed_at                 AS computed_at
            FROM curated_geo.indicator_result ir
            JOIN curated_geo.feature f ON f.id = ir.feature_id
            WHERE {' AND '.join(clauses)}
            ORDER BY ir.feature_id
            LIMIT ?
            """,
            params,
        ).fetchall()
        cols = (
            "feature_id",
            "type_slug",
            "geometry_wkb",
            "value_numeric",
            "quality_flag",
            "computed_at",
        )
        return [
            {
                **dict(zip(cols, row)),
                "geometry_wkb": bytes(row[2]) if row[2] is not None else b"",
            }
            for row in rows
        ]

    def value_stats(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        aoi_id: str | None = None,
    ) -> dict:
        clauses = [
            "ir.indicator_slug = ?",
            "ir.status = 'current'",
            "ir.value_numeric IS NOT NULL",
        ]
        params: list = [indicator_slug]
        clause, extra = _scenario_filter(
            "CAST(ir.scenario_id AS VARCHAR)", scenario_id, "?"
        )
        clauses.append(clause)
        params.extend(extra)
        if feature_type is not None:
            clauses.append("ir.feature_type = ?")
            params.append(feature_type)
        if aoi_id is not None:
            # Mirrors the Postgres EXISTS subquery; DuckDB's planner
            # handles it identically.
            clauses.append(
                "EXISTS (SELECT 1 FROM curated_geo.aoi_feature af "
                "WHERE af.feature_id = ir.feature_id "
                "AND CAST(af.aoi_id AS VARCHAR) = ?)"
            )
            params.append(aoi_id)

        row = self.connection.execute(
            f"""
            SELECT COUNT(*)                                AS count,
                   MIN(ir.value_numeric)                   AS min,
                   quantile_cont(ir.value_numeric, 0.05)   AS p05,
                   quantile_cont(ir.value_numeric, 0.50)   AS p50,
                   quantile_cont(ir.value_numeric, 0.95)   AS p95,
                   MAX(ir.value_numeric)                   AS max
            FROM curated_geo.indicator_result ir
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchone()
        count, vmin, p05, p50, p95, vmax = row
        return {
            "count": int(count),
            "min": float(vmin) if vmin is not None else None,
            "p05": float(p05) if p05 is not None else None,
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "max": float(vmax) if vmax is not None else None,
        }


_CURRENT_INDICATOR_V_COLS = (
    "id",
    "feature_id",
    "feature_type",
    "scenario_id",
    "indicator_id",
    "indicator_slug",
    "indicator_name",
    "unit",
    "value_type",
    "computation_kind",
    "job_run_id",
    "model_version",
    "value_numeric",
    "value_text",
    "value_json",
    "quality_flag",
    "confidence",
    "missing_reason",
    "computed_at",
)


def _row_as_dict_current_indicator(row: tuple) -> dict:
    return dict(zip(_CURRENT_INDICATOR_V_COLS, row))
