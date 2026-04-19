"""DuckDB repository implementations for local/dev storage."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pyarrow as pa

from .._utils import normalize_json_value as _normalize_json_value


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
                [storage_type, integration_role, storage_uri, license, notes, existing["id"]],
            )
            return str(existing["id"])

        source_id = str(uuid4())
        self.connection.execute(
            """
            INSERT INTO curated_geo.data_source_catalog
              (id, name, provider, storage_type, integration_role, storage_uri, version, license, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [source_id, name, provider, storage_type, integration_role, storage_uri, version, license, notes],
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
            [run_id, source_id, orchestrator, orchestrator_run_id, transformation_step, code_version],
        )
        return run_id

    def complete_ingestion_run(self, run_id: str, *, status: str, details: dict | None = None) -> None:
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
                    "parent_id": str(row[parent_id_col]) if parent_id_col and parent_id_col in row.index else None,
                    "name": str(row[name_col]) if name_col and name_col in row.index else None,
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
            present_attr_cols = [col for col in attributes_cols if col in available_columns]
        else:
            present_attr_cols = []
        if present_attr_cols:
            fields = ", ".join(
                f"{_quote_identifier(col)} := s.{_quote_identifier(col)}" for col in present_attr_cols
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

    def bulk_upsert_current(self, records: list[dict], *, source_id: str, ingestion_run_id: str) -> int:
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
            insert_rows.append([
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
            ])

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

        insert_table = pa.Table.from_pylist([
            {
                "id": row[0], "feature_id": row[1], "attribute_key": row[2],
                "value_type": row[3], "value_numeric": row[4], "value_text": row[5],
                "value_json": row[6], "source_id": row[7],
                "ingestion_run_id": row[8], "lineage_ref": row[9],
            }
            for row in insert_rows
        ])
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


class DuckDBAOIRepository:
    def __init__(self, connection):
        self.connection = connection

    def create_aoi(self, *, project_id: str, name: str, geometry_wkt: str, description: str | None = None) -> str:
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

        self.connection.execute("DELETE FROM curated_geo.aoi_feature WHERE aoi_id = ?", [aoi_id])

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
            insert_rows.append((aoi_id, feature_id, "area", overlap_measure, feature_area, overlap_ratio))

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
            insert_rows.append((aoi_id, feature_id, "length", overlap_measure, feature_len, overlap_ratio))

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


class DuckDBOverlapRepository:
    """Compute feature-to-feature spatial overlaps using geodesic measurements.

    Uses pyproj.Geod + shapely.wkb for WGS84 ellipsoid-accurate measurements,
    matching the PostgreSQL backend's ::geography behaviour.
    """

    def __init__(self, connection):
        self.connection = connection

    def compute_feature_overlaps(self, source_type_id: str, target_type_id: str) -> int:
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
                """
                SELECT
                    CAST(sf.id AS VARCHAR),
                    CAST(tf.id AS VARCHAR),
                    ST_AsWKB(sf.geometry),
                    ST_AsWKB(ST_Intersection(sf.geometry, tf.geometry))
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Intersects(sf.geometry, tf.geometry)
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, source_type_id],
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
                    insert_rows.append((src_id, tgt_id, "area", overlap_measure, feature_area, overlap_ratio))

        elif source_family == "line":
            result = self.connection.execute(
                """
                SELECT
                    CAST(sf.id AS VARCHAR),
                    CAST(tf.id AS VARCHAR),
                    ST_AsWKB(sf.geometry),
                    ST_AsWKB(ST_Intersection(sf.geometry, tf.geometry))
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Intersects(sf.geometry, tf.geometry)
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, source_type_id],
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
                    insert_rows.append((src_id, tgt_id, "length", overlap_measure, feature_len, overlap_ratio))

        else:  # point or geometry — presence basis
            pairs = self.connection.execute(
                """
                SELECT CAST(sf.id AS VARCHAR), CAST(tf.id AS VARCHAR)
                FROM curated_geo.feature sf
                JOIN curated_geo.feature tf
                    ON CAST(tf.type_id AS VARCHAR) = ?
                    AND ST_Within(sf.geometry, tf.geometry)
                WHERE CAST(sf.type_id AS VARCHAR) = ?
                """,
                [target_type_id, source_type_id],
            ).fetchall()
            insert_rows = [(src_id, tgt_id, "presence", 1.0, 1.0, 1.0) for src_id, tgt_id in pairs]

        if insert_rows:
            self.connection.executemany(_UPSERT_SQL, insert_rows)

        return len(insert_rows)


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
            extra_conditions = (" AND " + " AND ".join(conditions)) if conditions else ""
            result = self.connection.execute(
                f"""
                SELECT
                    CAST(f.id AS VARCHAR) AS id,
                    f.type_slug,
                    f.role,
                    f.name,
                    ST_AsText(f.geometry) AS geometry_wkt,
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
                    ST_AsText(f.geometry) AS geometry_wkt,
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
        for row in rows:
            if isinstance(row.get("attributes"), str):
                row["attributes"] = json.loads(row["attributes"])

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["created_at"], last["id"])

        for row in rows:
            row.pop("created_at", None)

        return {"features": rows, "next_cursor": next_cursor}


# Columns that are stored as JSON in the DB and returned as strings by DuckDB.
# Only these are JSON-decoded; other string values are left untouched.
_EXPLORE_JSON_COLUMNS: frozenset[str] = frozenset({
    "details",
    "attributes",
    "band_description",
    "schema_definition",
    "lineage_ref",
})


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
