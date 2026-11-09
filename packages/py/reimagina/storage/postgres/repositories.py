"""PostgreSQL/PostGIS repository implementations."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import psycopg2.extras
import pyarrow.parquet as pq

from .._utils import is_run_id as _is_run_id
from .._utils import normalize_json_value as _normalize_json_value
from .._utils import scenario_filter as _scenario_filter
from .._utils import scenario_overlay as _scenario_overlay


#: Smallest share of a feature that must fall inside an AOI for it to count as a
#: member. Guards ``compute_overlaps`` against float-noise membership: two
#: polygons that merely share a border line intersect to a sliver with a nonzero
#: but physically meaningless area, and the ``overlap_ratio > 0`` CHECK on
#: ``aoi_feature`` is not enough to reject it. Measured on Tlalpan: Coyoacan and
#: Xochimilco were members at ratios of 6.2e-12 and 8.1e-12 — 0.000000 % of each
#: municipality actually inside — and their entire polygons then rendered on an
#: AOI-scoped map.
#:
#: 1e-9 is not a guess. In that database the largest noise ratio is 8.1e-12 and
#: the smallest *genuine* overlap is 2.9e-06 (a straddling AGEB), and the band
#: between 1e-9 and 1e-6 contains **zero rows** — six orders of magnitude of
#: clear air on either side, so the threshold's exact position within it cannot
#: change the outcome.
#:
#: ⚠ This rejects noise, not partial membership. A feature genuinely straddling
#: the boundary stays a member at any real ratio — 42 AGEBs and 8 blocks do, and
#: they still render past the AOI edge. That is accepted behaviour, not an
#: oversight. Giving each feature exactly one AOI needs a majority-area rule
#: (``>= 0.5``), which is a decision about what an AOI *means* and would move
#: every AOI-scoped aggregate, so it is deliberately not made here.
MEMBERSHIP_MIN_OVERLAP_RATIO = 1e-9


class PostgresSourceCatalogRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.data_source_catalog
                  (name, provider, storage_type, integration_role, storage_uri, version, license, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, name, version) DO UPDATE
                   SET storage_type = EXCLUDED.storage_type,
                       integration_role = EXCLUDED.integration_role,
                       storage_uri = EXCLUDED.storage_uri,
                       license = COALESCE(EXCLUDED.license, curated_geo.data_source_catalog.license),
                       notes = COALESCE(EXCLUDED.notes, curated_geo.data_source_catalog.notes)
                RETURNING id
                """,
                (
                    name,
                    provider,
                    storage_type,
                    integration_role,
                    storage_uri,
                    version,
                    license,
                    notes,
                ),
            )
            return str(cur.fetchone()[0])

    def get_source(self, *, name: str, provider: str, version: str):
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, provider, storage_type, integration_role, storage_uri, version
                FROM curated_geo.data_source_catalog
                WHERE name = %s AND provider = %s AND version = %s
                """,
                (name, provider, version),
            )
            row = cur.fetchone()
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.data_source_catalog
                   SET storage_uri = %s
                 WHERE id = %s
                """,
                (storage_uri, source_id),
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.ingestion_run
                  (source_id, orchestrator, orchestrator_run_id, transformation_step, code_version, status)
                VALUES (%s, %s, %s, %s, %s, 'running')
                RETURNING id
                """,
                (
                    source_id,
                    orchestrator,
                    orchestrator_run_id,
                    transformation_step,
                    code_version,
                ),
            )
            return str(cur.fetchone()[0])

    def complete_ingestion_run(
        self, run_id: str, *, status: str, details: dict | None = None
    ) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.ingestion_run
                   SET status = %s, completed_at = now(), details = %s
                 WHERE id = %s
                """,
                (status, json.dumps(details or {}), run_id),
            )
            if status == "succeeded":
                # Refresh the serving snapshot so viewport reads see the new data.
                # Non-CONCURRENTLY runs inside the transaction and takes a brief
                # AccessExclusiveLock on the MV.  This is acceptable for an
                # ingestion pipeline where concurrent serving reads are sparse.
                # Switch to CONCURRENTLY (requires a separate autocommit connection)
                # once a dedicated background-refresh job is in place.
                cur.execute(
                    "REFRESH MATERIALIZED VIEW serving_geo.feature_current_attributes_mv"
                )


class PostgresFeatureRepository:
    def __init__(self, connection):
        self.connection = connection

    def seed_feature_types(self, rows: list[tuple]) -> None:
        with self.connection.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO curated_geo.feature_type_catalog
                  (context, type_slug, label, geometry_family, role, admin_level)
                VALUES %s
                ON CONFLICT (context, type_slug) DO NOTHING
                """,
                rows,
            )

    def get_type_id(self, context: str, type_slug: str) -> str:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM curated_geo.feature_type_catalog WHERE context = %s AND type_slug = %s",
                (context, type_slug),
            )
            row = cur.fetchone()
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

        rows = []
        for _, row in gdf.iterrows():
            attributes: dict = {}
            if attributes_cols:
                for col in attributes_cols:
                    if col in row.index and row[col] is not None:
                        attributes[col] = _normalize_json_value(row[col])
            rows.append(
                (
                    type_id,
                    str(row[parent_id_col])
                    if parent_id_col and parent_id_col in row.index
                    else None,
                    str(row[name_col]) if name_col and name_col in row.index else None,
                    row.geometry.wkt,
                    json.dumps(attributes, allow_nan=False),
                    source_id,
                    str(row[source_record_ref_col]),
                    ingestion_run_id,
                    dataset_version,
                )
            )

        if not rows:
            return 0

        with self.connection.cursor() as cur:
            # page_size=len(rows) forces a single VALUES statement so cur.rowcount
            # reflects the total rows inserted, not just the last page.
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO curated_geo.feature
                  (type_id, parent_id, name, geometry, attributes,
                   source_id, source_record_ref, ingestion_run_id, dataset_version)
                VALUES %s
                ON CONFLICT (source_id, dataset_version, source_record_ref) DO NOTHING
                """,
                rows,
                template=(
                    "(%s, %s, %s,"
                    " ST_SetSRID(ST_GeomFromText(%s), 4326),"
                    " %s::jsonb, %s, %s, %s, %s)"
                ),
                page_size=len(rows),
            )
            return cur.rowcount

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
        """Stream features from GeoParquet into PostGIS in bounded batches."""
        parquet_file = pq.ParquetFile(str(Path(parquet_path)))
        available_columns = set(parquet_file.schema_arrow.names)
        if source_record_ref_col not in available_columns:
            raise ValueError(
                f"Required source record column {source_record_ref_col!r} is missing from {parquet_path!r}"
            )

        requested_columns = ["geometry", source_record_ref_col]
        for optional in [name_col, parent_id_col, *(attributes_cols or [])]:
            if (
                optional
                and optional in available_columns
                and optional not in requested_columns
            ):
                requested_columns.append(optional)

        batch_size = 10_000
        inserted_count = 0
        for batch in parquet_file.iter_batches(
            batch_size=batch_size, columns=requested_columns
        ):
            rows = []
            for record in batch.to_pylist():
                attributes: dict = {}
                for col in attributes_cols or []:
                    if col in record:
                        normalized = _normalize_json_value(record[col])
                        if normalized is not None:
                            attributes[col] = normalized

                rows.append(
                    (
                        type_id,
                        str(record[parent_id_col])
                        if parent_id_col and record.get(parent_id_col)
                        else None,
                        str(record[name_col])
                        if name_col and record.get(name_col) is not None
                        else None,
                        psycopg2.Binary(record["geometry"])
                        if record.get("geometry") is not None
                        else None,
                        json.dumps(attributes, allow_nan=False),
                        source_id,
                        str(record[source_record_ref_col]),
                        ingestion_run_id,
                        dataset_version,
                    )
                )

            if not rows:
                continue

            with self.connection.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO curated_geo.feature
                      (type_id, parent_id, name, geometry, attributes,
                       source_id, source_record_ref, ingestion_run_id, dataset_version)
                    VALUES %s
                    ON CONFLICT (source_id, dataset_version, source_record_ref) DO NOTHING
                    """,
                    rows,
                    template=(
                        "(%s, %s, %s,"
                        " ST_SetSRID(ST_GeomFromWKB(%s), 4326),"
                        " %s::jsonb, %s, %s, %s, %s)"
                    ),
                    page_size=batch_size,
                )
                inserted_count += max(cur.rowcount, 0)
        return inserted_count

    def geodesic_metrics_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[tuple[str, float, float]]:
        # ::geography gives geodesic area/perimeter directly; no reprojection
        # needed. AOI-scoped variant uses the GIST index on AOI + feature.
        # feature_ids narrows further to the selective-recompute set.
        id_filter = " AND f.id = ANY(%s::uuid[])" if feature_ids else ""
        with self.connection.cursor() as cur:
            if aoi_id is None:
                params: list = [type_id]
                if feature_ids:
                    params.append(list(feature_ids))
                cur.execute(
                    f"""
                    SELECT f.id::text,
                           ST_Area(f.geometry::geography),
                           ST_Perimeter(f.geometry::geography)
                    FROM curated_geo.feature f
                    WHERE f.type_id = %s{id_filter}
                    """,
                    params,
                )
            else:
                params = [aoi_id, type_id]
                if feature_ids:
                    params.append(list(feature_ids))
                cur.execute(
                    f"""
                    SELECT f.id::text,
                           ST_Area(f.geometry::geography),
                           ST_Perimeter(f.geometry::geography)
                    FROM curated_geo.feature f
                    JOIN curated_geo.area_of_interest aoi
                      ON aoi.id = %s AND ST_Intersects(f.geometry, aoi.geometry)
                    WHERE f.type_id = %s{id_filter}
                    """,
                    params,
                )
            return [
                (str(fid), float(area), float(perim))
                for fid, area, perim in cur.fetchall()
            ]

    def iter_geometries_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        with self.connection.cursor() as cur:
            if aoi_id is None:
                cur.execute(
                    """
                    SELECT f.id::text, ST_AsBinary(f.geometry)
                    FROM curated_geo.feature f
                    WHERE f.type_id = %s
                    """,
                    (type_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT f.id::text, ST_AsBinary(f.geometry)
                    FROM curated_geo.feature f
                    JOIN curated_geo.area_of_interest aoi
                      ON aoi.id = %s AND ST_Intersects(f.geometry, aoi.geometry)
                    WHERE f.type_id = %s
                    """,
                    (aoi_id, type_id),
                )
            return [(str(fid), bytes(wkb)) for fid, wkb in cur.fetchall()]

    def find_by_attributes(
        self,
        *,
        type_slug: str,
        attributes: dict[str, str],
    ) -> dict[str, str] | None:
        if not attributes:
            raise ValueError("find_by_attributes requires at least one attribute")
        # Build one `attributes->>'key' = %s` predicate per input entry.
        predicates = [f"attributes->>'{key}' = %s" for key in attributes]
        params: list = list(attributes.values()) + [type_slug]
        with self.connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT id::text, ST_AsText(geometry)
                FROM curated_geo.feature
                WHERE {" AND ".join(predicates)}
                  AND type_slug = %s
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
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
        id_filter = " AND parent.id = ANY(%s::uuid[])" if feature_ids else ""
        with self.connection.cursor() as cur:
            if aoi_id is None:
                params: list = [child_type_id, parent_type_id]
                if feature_ids:
                    params.append(list(feature_ids))
                cur.execute(
                    f"""
                    SELECT parent.id::text,
                           COUNT(child.id)::int,
                           ST_Area(parent.geometry::geography)
                    FROM curated_geo.feature parent
                    LEFT JOIN curated_geo.feature child
                           ON child.type_id = %s
                          AND parent.geometry && child.geometry
                          AND ST_Within(ST_Centroid(child.geometry), parent.geometry)
                    WHERE parent.type_id = %s{id_filter}
                    GROUP BY parent.id, parent.geometry
                    """,
                    params,
                )
            else:
                params = [aoi_id, child_type_id, parent_type_id]
                if feature_ids:
                    params.append(list(feature_ids))
                cur.execute(
                    f"""
                    SELECT parent.id::text,
                           COUNT(child.id)::int,
                           ST_Area(parent.geometry::geography)
                    FROM curated_geo.feature parent
                    JOIN curated_geo.area_of_interest aoi
                      ON aoi.id = %s AND ST_Intersects(parent.geometry, aoi.geometry)
                    LEFT JOIN curated_geo.feature child
                           ON child.type_id = %s
                          AND parent.geometry && child.geometry
                          AND ST_Within(ST_Centroid(child.geometry), parent.geometry)
                    WHERE parent.type_id = %s{id_filter}
                    GROUP BY parent.id, parent.geometry
                    """,
                    params,
                )
            return [
                (str(fid), int(count), float(area))
                for fid, count, area in cur.fetchall()
            ]


class PostgresObservationRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.observation
                   SET is_current = false, valid_to = now(), updated_at = now()
                 WHERE feature_id = %s AND attribute_key = %s AND is_current
                """,
                (feature_id, attribute_key),
            )
            cur.execute(
                """
                INSERT INTO curated_geo.observation
                  (feature_id, attribute_key, value_type,
                   value_numeric, value_text, value_json,
                   source_id, ingestion_run_id, lineage_ref, is_current)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, true)
                RETURNING id
                """,
                (
                    feature_id,
                    attribute_key,
                    value_type,
                    value_numeric,
                    value_text,
                    value_json,
                    source_id,
                    ingestion_run_id,
                    json.dumps(lineage_ref) if lineage_ref else None,
                ),
            )
            return str(cur.fetchone()[0])

    def bulk_upsert_current(
        self, records: list[dict], *, source_id: str, ingestion_run_id: str
    ) -> int:
        if not records:
            return 0

        # Build retire pairs and insert rows in one pass.
        retire_pairs: list[tuple] = []
        insert_rows: list[tuple] = []
        for r in records:
            retire_pairs.append((r["feature_id"], r["attribute_key"]))
            value_numeric = r["value"] if r["value_type"] == "numeric" else None
            value_text = r["value"] if r["value_type"] == "text" else None
            value_json = json.dumps(r["value"]) if r["value_type"] == "json" else None
            insert_rows.append(
                (
                    r["feature_id"],
                    r["attribute_key"],
                    r["value_type"],
                    value_numeric,
                    value_text,
                    value_json,
                    source_id,
                    ingestion_run_id,
                    json.dumps(r["lineage_ref"]) if r.get("lineage_ref") else None,
                )
            )

        with self.connection.cursor() as cur:
            # Retire existing current rows in one statement via a VALUES join.
            psycopg2.extras.execute_values(
                cur,
                """
                UPDATE curated_geo.observation o
                   SET is_current = false, valid_to = now(), updated_at = now()
                  FROM (VALUES %s) AS v(fid, akey)
                 WHERE o.feature_id = v.fid::uuid
                   AND o.attribute_key = v.akey
                   AND o.is_current
                """,
                retire_pairs,
                page_size=len(retire_pairs),
            )
            # Insert all new current rows in one statement.
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO curated_geo.observation
                  (feature_id, attribute_key, value_type,
                   value_numeric, value_text, value_json,
                   source_id, ingestion_run_id, lineage_ref, is_current)
                VALUES %s
                """,
                insert_rows,
                template=("(%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, true)"),
                page_size=len(insert_rows),
            )
        return len(records)

    def retire_current_by_prefix(
        self,
        *,
        prefix: str,
        feature_type_id: str,
        aoi_id: str | None = None,
    ) -> int:
        # Match either the exact prefix OR `<prefix>_<anything>`. We
        # intentionally avoid LIKE + ESCAPE here — Postgres E-strings
        # silently swallow unknown backslash sequences (`\_` ≠ escape),
        # which would turn an "escape the underscore" pattern into an
        # unescaped wildcard and retire `ndvi2_mean` alongside
        # `ndvi_mean`. `starts_with()` (Postgres 11+) has no such trap.
        if not prefix:
            raise ValueError("retire_current_by_prefix: prefix must be non-empty")
        if not feature_type_id:
            # Required, not defaulted. This retires rows; a caller that forgot
            # to say which grain it recomputed would wipe every other grain's
            # values for the prefix, which is exactly the bug this parameter
            # exists to close. There is no sensible "all grains" default for a
            # producer that only ever writes one.
            raise ValueError(
                "retire_current_by_prefix: feature_type_id must be non-empty — "
                "retirement is scoped to the grain the run recomputed"
            )
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.observation o
                   SET is_current = false, valid_to = now(), updated_at = now()
                 WHERE o.is_current
                   AND (o.attribute_key = %(prefix)s
                        OR starts_with(o.attribute_key, %(prefix)s || '_'))
                   AND EXISTS (
                         SELECT 1 FROM curated_geo.feature f
                          WHERE f.id = o.feature_id
                            AND f.type_id = %(type_id)s::uuid)
                   AND (%(aoi_id)s::uuid IS NULL
                        OR EXISTS (
                             SELECT 1 FROM curated_geo.aoi_feature af
                              WHERE af.feature_id = o.feature_id
                                AND af.aoi_id = %(aoi_id)s::uuid))
                """,
                {"prefix": prefix, "type_id": feature_type_id, "aoi_id": aoi_id},
            )
            return cur.rowcount

    def has_active_prefix(
        self,
        *,
        prefix: str,
        excluding_source_id: str,
        feature_type_id: str,
    ) -> bool:
        # Scoped to the same grain `retire_current_by_prefix` is scoped to, and
        # deliberately NOT to an AOI. The guard has to cover the widest thing a
        # run could retire, and an AOI is only ever a narrowing the caller opts
        # into: a whole-grain run retires the whole grain, so a collision
        # outside some AOI is still a collision. Scoping by grain is what stops
        # the guard refusing two recipes that own one prefix at two grains —
        # which aggregation_recipe_prefix_uk (r020) explicitly permits, and
        # which grain-scoped retirement makes safe.
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM curated_geo.observation o
                WHERE o.is_current
                  AND o.source_id <> %(source_id)s
                  AND (o.attribute_key = %(prefix)s
                       OR starts_with(o.attribute_key, %(prefix)s || '_'))
                  AND EXISTS (
                        SELECT 1 FROM curated_geo.feature f
                         WHERE f.id = o.feature_id
                           AND f.type_id = %(type_id)s::uuid)
                LIMIT 1
                """,
                {
                    "source_id": excluding_source_id,
                    "prefix": prefix,
                    "type_id": feature_type_id,
                },
            )
            return cur.fetchone() is not None


class PostgresFileAssetRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.file_asset
                  (source_id, ingestion_run_id, storage_uri, format, crs,
                   resolution_m, band_description, captured_at, dataset_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING id
                """,
                (
                    source_id,
                    ingestion_run_id,
                    storage_uri,
                    format,
                    crs,
                    resolution_m,
                    json.dumps(band_description or {}),
                    captured_at,
                    dataset_version,
                ),
            )
            return str(cur.fetchone()[0])

    def get_by_source(self, source_id: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id::text, source_id::text, ingestion_run_id::text,
                       storage_uri, format, crs, resolution_m,
                       band_description, captured_at, dataset_version,
                       created_at
                FROM curated_geo.file_asset
                WHERE source_id = %s
                """,
                (source_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


class PostgresRasterLayerRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.raster_layer
                  (source_id, asset_id, layer_slug, logical_name, description,
                   colormap_name, rescale, minzoom, maxzoom, published,
                   aggregation_kind)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
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
                ),
            )
            return str(cur.fetchone()[0])

    def get_by_source(self, source_id: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id::text, source_id::text, asset_id::text,
                       layer_slug, logical_name, description,
                       colormap_name, rescale, minzoom, maxzoom,
                       published, aggregation_kind, created_at,
                       storage_uri, crs, resolution_m, format,
                       band_description
                FROM serving_geo.raster_layer_v
                WHERE source_id = %s
                """,
                (source_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_by_id(self, layer_id: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id::text, source_id::text, asset_id::text,
                       layer_slug, logical_name, description,
                       colormap_name, rescale, minzoom, maxzoom,
                       published, aggregation_kind, created_at,
                       storage_uri, crs, resolution_m, format,
                       band_description
                FROM serving_geo.raster_layer_v
                WHERE id = %s
                """,
                (layer_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_published_layers(self) -> list[dict]:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id::text, source_id::text, asset_id::text,
                       layer_slug, logical_name, description,
                       colormap_name, rescale, minzoom, maxzoom,
                       published, aggregation_kind, created_at,
                       storage_uri, crs, resolution_m, format
                FROM serving_geo.raster_layer_v
                WHERE published
                ORDER BY layer_slug
                """
            )
            return [dict(row) for row in cur.fetchall()]

    def set_published(self, *, layer_slug: str, published: bool) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.raster_layer
                   SET published = %s
                 WHERE layer_slug = %s
                """,
                (published, layer_slug),
            )


class PostgresAggregationRecipeRepository:
    """Postgres-backed recipe registry (r020). Reads via serving view."""

    _SELECT_COLS = (
        "id::text, input_kind, "
        "input_raster_layer_id::text, input_raster_layer_slug, "
        "input_attribute_key_prefix, input_feature_type_slug, "
        "target_feature_type_slug, attribute_key_prefix, "
        "method, weight_basis, params, coverage_threshold, "
        "aoi_id::text, enabled, created_at, updated_at"
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
        coverage_threshold: float = 0.0,
        aoi_id: str | None = None,
        enabled: bool = True,
    ) -> str:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.aggregation_recipe
                  (input_kind, input_raster_layer_id, input_attribute_key_prefix,
                   input_feature_type_slug, target_feature_type_slug,
                   attribute_key_prefix, method, weight_basis, params,
                   coverage_threshold, aoi_id, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING id
                """,
                (
                    input_kind,
                    input_raster_layer_id,
                    input_attribute_key_prefix,
                    input_feature_type_slug,
                    target_feature_type_slug,
                    attribute_key_prefix,
                    method,
                    weight_basis,
                    psycopg2.extras.Json(params or {}),
                    coverage_threshold,
                    aoi_id,
                    enabled,
                ),
            )
            return str(cur.fetchone()[0])

    def get_recipe(self, recipe_id: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"SELECT {self._SELECT_COLS} "
                f"FROM serving_geo.aggregation_recipe_v WHERE id = %s",
                (recipe_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_recipes(
        self,
        *,
        input_raster_layer_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list = []
        if input_raster_layer_id is not None:
            conditions.append("input_raster_layer_id = %s")
            params.append(input_raster_layer_id)
        if enabled_only:
            conditions.append("enabled")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"SELECT {self._SELECT_COLS} "
                f"FROM serving_geo.aggregation_recipe_v{where} "
                f"ORDER BY target_feature_type_slug, attribute_key_prefix",
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def set_enabled(self, *, recipe_id: str, enabled: bool) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE curated_geo.aggregation_recipe
                   SET enabled = %s, updated_at = now()
                 WHERE id = %s
                """,
                (enabled, recipe_id),
            )

    def delete_recipe(self, recipe_id: str) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM curated_geo.aggregation_recipe WHERE id = %s",
                (recipe_id,),
            )


class PostgresAOIRepository:
    def __init__(self, connection):
        self.connection = connection

    def create_aoi(
        self,
        *,
        project_id: str,
        name: str,
        geometry_wkt: str,
        description: str | None = None,
        membership_key: dict[str, str] | None = None,
    ) -> str:
        """``membership_key`` declares identifier-based membership (r040).

        Pass it whenever the AOI is derived from an administrative unit, so
        features carrying the same identifiers are placed exactly rather than
        by spatial overlap. Leave it None for a drawn or arbitrary AOI, which
        has no identifier to match on and must be decided geometrically.
        """
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.area_of_interest
                  (project_id, name, description, geometry, membership_key)
                VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s)
                RETURNING id
                """,
                (
                    project_id,
                    name,
                    description,
                    geometry_wkt,
                    psycopg2.extras.Json(membership_key) if membership_key else None,
                ),
            )
            return str(cur.fetchone()[0])

    def compute_overlaps(self, aoi_id: str) -> int:
        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM curated_geo.aoi_feature WHERE aoi_id = %s", (aoi_id,)
            )
            cur.execute(
                """
                WITH aoi_raw AS (
                  SELECT geometry, membership_key
                  FROM curated_geo.area_of_interest
                  WHERE id = %s
                ),
                aoi_fixed AS (
                  SELECT
                    CASE
                      WHEN ST_IsValid(geometry) THEN geometry
                      ELSE ST_CollectionExtract(ST_MakeValid(geometry), 3)
                    END AS geometry
                  FROM aoi_raw
                ),
                feature_candidates AS (
                  SELECT
                    f.id,
                    f.geometry
                  FROM curated_geo.feature f
                  CROSS JOIN aoi_raw aoi
                  WHERE ST_GeometryType(f.geometry) IN ('ST_Polygon','ST_MultiPolygon')
                    AND f.geometry && aoi.geometry
                    -- Identifiers beat geometry where both exist. A feature
                    -- carrying every key the AOI declares is a member iff the
                    -- values match exactly; one that carries none of them
                    -- (Overture buildings, OSM establishments) is decided
                    -- spatially as before. See migration r040.
                    AND (
                      aoi.membership_key IS NULL
                      OR NOT (f.attributes ?& ARRAY(
                           SELECT jsonb_object_keys(aoi.membership_key)))
                      OR f.attributes @> aoi.membership_key
                    )
                ),
                feature_fixed AS (
                  SELECT
                    f.id,
                    CASE
                      WHEN ST_IsValid(f.geometry) THEN f.geometry
                      ELSE ST_CollectionExtract(ST_MakeValid(f.geometry), 3)
                    END AS geometry
                  FROM feature_candidates f
                ),
                intersections AS (
                  SELECT
                    f.id,
                    f.geometry AS feature_geometry,
                    ST_Intersection(f.geometry, aoi.geometry) AS overlap_geometry
                  FROM feature_fixed f
                  CROSS JOIN aoi_fixed aoi
                  WHERE NOT ST_IsEmpty(f.geometry)
                    AND NOT ST_IsEmpty(aoi.geometry)
                    AND ST_Intersects(f.geometry, aoi.geometry)
                )
                INSERT INTO curated_geo.aoi_feature
                  (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
                SELECT
                  %s AS aoi_id,
                  i.id,
                  'area',
                  LEAST(
                    ST_Area(i.overlap_geometry::geography),
                    ST_Area(i.feature_geometry::geography)
                  ),
                  ST_Area(i.feature_geometry::geography),
                  LEAST(
                    ST_Area(i.overlap_geometry::geography)
                      / NULLIF(ST_Area(i.feature_geometry::geography), 0),
                    1.0
                  )
                FROM intersections i
                WHERE ST_Area(i.overlap_geometry::geography)
                        / NULLIF(ST_Area(i.feature_geometry::geography), 0)
                      >= %s
                """,
                (aoi_id, aoi_id, MEMBERSHIP_MIN_OVERLAP_RATIO),
            )
            poly_count = cur.rowcount
            cur.execute(
                """
                WITH aoi_raw AS (
                  SELECT geometry, membership_key
                  FROM curated_geo.area_of_interest
                  WHERE id = %s
                ),
                aoi_fixed AS (
                  SELECT
                    CASE
                      WHEN ST_IsValid(geometry) THEN geometry
                      ELSE ST_CollectionExtract(ST_MakeValid(geometry), 3)
                    END AS geometry
                  FROM aoi_raw
                ),
                feature_candidates AS (
                  SELECT
                    f.id,
                    f.geometry
                  FROM curated_geo.feature f
                  CROSS JOIN aoi_raw aoi
                  WHERE ST_GeometryType(f.geometry) IN ('ST_LineString','ST_MultiLineString')
                    AND f.geometry && aoi.geometry
                    -- Identifiers beat geometry where both exist. A feature
                    -- carrying every key the AOI declares is a member iff the
                    -- values match exactly; one that carries none of them
                    -- (Overture buildings, OSM establishments) is decided
                    -- spatially as before. See migration r040.
                    AND (
                      aoi.membership_key IS NULL
                      OR NOT (f.attributes ?& ARRAY(
                           SELECT jsonb_object_keys(aoi.membership_key)))
                      OR f.attributes @> aoi.membership_key
                    )
                ),
                feature_fixed AS (
                  SELECT
                    f.id,
                    CASE
                      WHEN ST_IsValid(f.geometry) THEN f.geometry
                      ELSE ST_CollectionExtract(ST_MakeValid(f.geometry), 2)
                    END AS geometry
                  FROM feature_candidates f
                ),
                intersections AS (
                  SELECT
                    f.id,
                    f.geometry AS feature_geometry,
                    ST_Intersection(f.geometry, aoi.geometry) AS overlap_geometry
                  FROM feature_fixed f
                  CROSS JOIN aoi_fixed aoi
                  WHERE NOT ST_IsEmpty(f.geometry)
                    AND NOT ST_IsEmpty(aoi.geometry)
                    AND ST_Intersects(f.geometry, aoi.geometry)
                )
                INSERT INTO curated_geo.aoi_feature
                  (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
                SELECT
                  %s AS aoi_id,
                  i.id,
                  'length',
                  LEAST(
                    ST_Length(i.overlap_geometry::geography),
                    ST_Length(i.feature_geometry::geography)
                  ),
                  ST_Length(i.feature_geometry::geography),
                  LEAST(
                    ST_Length(i.overlap_geometry::geography)
                      / NULLIF(ST_Length(i.feature_geometry::geography), 0),
                    1.0
                  )
                FROM intersections i
                WHERE ST_Length(i.overlap_geometry::geography)
                        / NULLIF(ST_Length(i.feature_geometry::geography), 0)
                      >= %s
                """,
                (aoi_id, aoi_id, MEMBERSHIP_MIN_OVERLAP_RATIO),
            )
            line_count = cur.rowcount
            cur.execute(
                """
                INSERT INTO curated_geo.aoi_feature
                  (aoi_id, feature_id, overlap_basis, overlap_measure, feature_measure, overlap_ratio)
                SELECT
                  %s AS aoi_id,
                  f.id,
                  'presence',
                  1, 1, 1
                FROM curated_geo.feature f
                JOIN curated_geo.area_of_interest aoi ON aoi.id = %s
                WHERE ST_GeometryType(f.geometry) IN ('ST_Point','ST_MultiPoint')
                  AND ST_Within(f.geometry, aoi.geometry)
                  -- Same identifier rule as the polygon and line families, so
                  -- the three cannot disagree about who is a member. No point
                  -- layer carries admin codes today; keeping the rule uniform
                  -- is cheaper than discovering the inconsistency when one does.
                  AND (
                    aoi.membership_key IS NULL
                    OR NOT (f.attributes ?& ARRAY(
                         SELECT jsonb_object_keys(aoi.membership_key)))
                    OR f.attributes @> aoi.membership_key
                  )
                """,
                (aoi_id, aoi_id),
            )
            point_count = cur.rowcount
        return poly_count + line_count + point_count

    def list_aois(self) -> list[dict]:
        with self.connection.cursor() as cur:
            # Single-pass aggregation. The earlier shape ran six
            # correlated subqueries (centroid lon/lat + four extent
            # corners), each re-doing the
            # ``aoi_feature × feature`` join — Postgres won't merge
            # them because they're independent scalar subqueries. With
            # 1.7 M aoi_feature rows that adds up to ~2.7 s. Folding
            # everything into one CTE that does the join + aggregation
            # once drops the endpoint to <100 ms.
            cur.execute(
                """
                WITH block_stats AS (
                    SELECT
                        af.aoi_id,
                        ST_Centroid(ST_Collect(ST_Centroid(f.geometry))) AS centroid,
                        ST_Extent(f.geometry) AS extent_box
                    FROM curated_geo.aoi_feature af
                    JOIN curated_geo.feature f ON f.id = af.feature_id
                    WHERE f.type_slug = 'block'
                    GROUP BY af.aoi_id
                )
                SELECT aoi.id::text, aoi.project_id::text, aoi.name, aoi.description,
                       -- AOI polygon bbox in EPSG:4326.
                       ST_XMin(aoi.geometry) AS min_lon,
                       ST_YMin(aoi.geometry) AS min_lat,
                       ST_XMax(aoi.geometry) AS max_lon,
                       ST_YMax(aoi.geometry) AS max_lat,
                       -- Urban-core centroid: count-weighted average of
                       -- block centroids inside the AOI. Falls back to
                       -- ST_PointOnSurface(geometry) when the AOI has
                       -- no overlapping blocks yet (fresh ingest).
                       COALESCE(ST_X(bs.centroid),
                                ST_X(ST_PointOnSurface(aoi.geometry))) AS centroid_lon,
                       COALESCE(ST_Y(bs.centroid),
                                ST_Y(ST_PointOnSurface(aoi.geometry))) AS centroid_lat,
                       -- Features bbox = envelope of all blocks loaded
                       -- inside this AOI. Distinct from the polygon
                       -- bbox above: a muni like Mexicali is mostly
                       -- desert with the populated area in one corner,
                       -- so the polygon bbox would frame mostly empty
                       -- land. NULL when no blocks are loaded yet —
                       -- frontend falls back to the polygon bbox.
                       ST_XMin(bs.extent_box) AS feat_min_lon,
                       ST_YMin(bs.extent_box) AS feat_min_lat,
                       ST_XMax(bs.extent_box) AS feat_max_lon,
                       ST_YMax(bs.extent_box) AS feat_max_lat
                FROM curated_geo.area_of_interest aoi
                LEFT JOIN block_stats bs ON bs.aoi_id = aoi.id
                ORDER BY aoi.name
                """
            )
            rows = cur.fetchall()
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, project_id::text, name, description
                FROM curated_geo.area_of_interest
                WHERE id = %s
                """,
                (aoi_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            aid, pid, name, descr = row
            return {
                "aoi_id": aid,
                "project_id": pid,
                "name": name,
                "description": descr,
            }

    def upsert_project_by_name(self, name: str, description: str | None = None) -> str:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM app_core.project WHERE name = %s LIMIT 1",
                (name,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return str(existing[0])
            cur.execute(
                "INSERT INTO app_core.project (name, description) VALUES (%s, %s) RETURNING id::text",
                (name, description),
            )
            return str(cur.fetchone()[0])


class PostgresOverlapRepository:
    """Compute feature-to-feature spatial overlaps using PostGIS geography measurements."""

    def __init__(self, connection):
        self.connection = connection

    def compute_feature_overlaps(
        self,
        source_type_id: str,
        target_type_id: str,
        *,
        aoi_id: str | None = None,
    ) -> int:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT geometry_family FROM curated_geo.feature_type_catalog WHERE id = %s",
                (source_type_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Unknown source_type_id: {source_type_id!r}")
            source_family: str = row[0]

            cur.execute(
                "SELECT id FROM curated_geo.feature_type_catalog WHERE id = %s",
                (target_type_id,),
            )
            if cur.fetchone() is None:
                raise ValueError(f"Unknown target_type_id: {target_type_id!r}")

            if aoi_id is not None:
                cur.execute(
                    "SELECT 1 FROM curated_geo.area_of_interest WHERE id = %s",
                    (aoi_id,),
                )
                if cur.fetchone() is None:
                    raise ValueError(f"Unknown aoi_id: {aoi_id!r}")

            aoi_join = (
                " JOIN curated_geo.area_of_interest aoi"
                "   ON aoi.id = %s AND ST_Intersects(sf.geometry, aoi.geometry)"
                if aoi_id is not None
                else ""
            )
            aoi_params: tuple = (aoi_id,) if aoi_id is not None else ()

            if source_family == "polygon":
                cur.execute(
                    f"""
                    INSERT INTO curated_geo.feature_overlap
                      (source_feature_id, target_feature_id, overlap_basis,
                       overlap_measure, feature_measure, overlap_ratio)
                    SELECT
                        sf.id,
                        tf.id,
                        'area',
                        LEAST(ST_Area(ST_Intersection(sf.geometry, tf.geometry)::geography),
                              ST_Area(sf.geometry::geography)),
                        ST_Area(sf.geometry::geography),
                        LEAST(
                            ST_Area(ST_Intersection(sf.geometry, tf.geometry)::geography)
                                / NULLIF(ST_Area(sf.geometry::geography), 0),
                            1.0
                        )
                    FROM curated_geo.feature sf
                    JOIN curated_geo.feature tf
                        ON tf.type_id = %s
                        AND sf.geometry && tf.geometry
                        AND ST_Intersects(sf.geometry, tf.geometry){aoi_join}
                    WHERE sf.type_id = %s
                      AND ST_Area(sf.geometry::geography) > 0
                      AND ST_Area(ST_Intersection(sf.geometry, tf.geometry)::geography) > 0
                    ON CONFLICT (source_feature_id, target_feature_id) DO UPDATE
                        SET overlap_basis    = EXCLUDED.overlap_basis,
                            overlap_measure  = EXCLUDED.overlap_measure,
                            feature_measure  = EXCLUDED.feature_measure,
                            overlap_ratio    = EXCLUDED.overlap_ratio,
                            computed_at      = now()
                    """,
                    (target_type_id, *aoi_params, source_type_id),
                )
            elif source_family == "line":
                cur.execute(
                    f"""
                    INSERT INTO curated_geo.feature_overlap
                      (source_feature_id, target_feature_id, overlap_basis,
                       overlap_measure, feature_measure, overlap_ratio)
                    SELECT
                        sf.id,
                        tf.id,
                        'length',
                        LEAST(ST_Length(ST_Intersection(sf.geometry, tf.geometry)::geography),
                              ST_Length(sf.geometry::geography)),
                        ST_Length(sf.geometry::geography),
                        LEAST(
                            ST_Length(ST_Intersection(sf.geometry, tf.geometry)::geography)
                                / NULLIF(ST_Length(sf.geometry::geography), 0),
                            1.0
                        )
                    FROM curated_geo.feature sf
                    JOIN curated_geo.feature tf
                        ON tf.type_id = %s
                        AND sf.geometry && tf.geometry
                        AND ST_Intersects(sf.geometry, tf.geometry){aoi_join}
                    WHERE sf.type_id = %s
                      AND ST_Length(sf.geometry::geography) > 0
                      AND ST_Length(ST_Intersection(sf.geometry, tf.geometry)::geography) > 0
                    ON CONFLICT (source_feature_id, target_feature_id) DO UPDATE
                        SET overlap_basis    = EXCLUDED.overlap_basis,
                            overlap_measure  = EXCLUDED.overlap_measure,
                            feature_measure  = EXCLUDED.feature_measure,
                            overlap_ratio    = EXCLUDED.overlap_ratio,
                            computed_at      = now()
                    """,
                    (target_type_id, *aoi_params, source_type_id),
                )
            else:  # point or geometry — presence basis
                cur.execute(
                    f"""
                    INSERT INTO curated_geo.feature_overlap
                      (source_feature_id, target_feature_id, overlap_basis,
                       overlap_measure, feature_measure, overlap_ratio)
                    SELECT sf.id, tf.id, 'presence', 1, 1, 1
                    FROM curated_geo.feature sf
                    JOIN curated_geo.feature tf
                        ON tf.type_id = %s
                        AND ST_Within(sf.geometry, tf.geometry){aoi_join}
                    WHERE sf.type_id = %s
                    ON CONFLICT (source_feature_id, target_feature_id) DO UPDATE
                        SET overlap_basis    = EXCLUDED.overlap_basis,
                            overlap_measure  = EXCLUDED.overlap_measure,
                            feature_measure  = EXCLUDED.feature_measure,
                            overlap_ratio    = EXCLUDED.overlap_ratio,
                            computed_at      = now()
                    """,
                    (target_type_id, *aoi_params, source_type_id),
                )

            return cur.rowcount

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
            aoi_clause = " AND fo.source_feature_id IN (SELECT feature_id FROM curated_geo.aoi_feature WHERE aoi_id = %s)"
            aoi_params.append(aoi_id)
        with self.connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT fo.target_feature_id::text,
                       obs.attribute_key,
                       obs.value_numeric,
                       fo.overlap_measure
                FROM curated_geo.feature_overlap fo
                JOIN curated_geo.feature src ON src.id = fo.source_feature_id
                     AND src.type_slug = %s
                JOIN curated_geo.feature tgt ON tgt.id = fo.target_feature_id
                     AND tgt.type_slug = %s
                JOIN curated_geo.observation obs
                     ON obs.feature_id = fo.source_feature_id
                    AND obs.is_current
                    AND obs.value_numeric IS NOT NULL
                    AND (obs.attribute_key = %s
                         OR obs.attribute_key LIKE %s ESCAPE '\\')
                WHERE TRUE{aoi_clause}
                """,
                (
                    source_feature_type,
                    target_feature_type,
                    input_attribute_key_prefix,
                    input_attribute_key_prefix + "\\_%",
                    *aoi_params,
                ),
            )
            return [
                (str(target_id), str(attr_key), float(value), float(weight))
                for target_id, attr_key, value, weight in cur.fetchall()
                if value is not None
            ]


class PostgresOverlapTypePairRepository:
    """Declared (source_type, target_type) pairs on Postgres."""

    def __init__(self, connection):
        self.connection = connection

    def upsert_pair(self, source_type_id: str, target_type_id: str) -> str:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.feature_overlap_type_pair
                  (source_type_id, target_type_id)
                VALUES (%s, %s)
                ON CONFLICT (source_type_id, target_type_id) DO UPDATE
                  SET source_type_id = EXCLUDED.source_type_id
                RETURNING id
                """,
                (source_type_id, target_type_id),
            )
            return str(cur.fetchone()[0])

    def list_pairs(self) -> list[dict]:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT
                    p.id::text             AS pair_id,
                    p.source_type_id::text AS source_type_id,
                    sc.context || ':' || sc.type_slug AS source,
                    p.target_type_id::text AS target_type_id,
                    tc.context || ':' || tc.type_slug AS target
                FROM curated_geo.feature_overlap_type_pair p
                JOIN curated_geo.feature_type_catalog sc ON sc.id = p.source_type_id
                JOIN curated_geo.feature_type_catalog tc ON tc.id = p.target_type_id
                ORDER BY sc.context, sc.type_slug, tc.context, tc.type_slug
                """
            )
            return [dict(row) for row in cur.fetchall()]


class PostgresServingReader:
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
            conditions.append("f.type_slug = %s")
            params.append(type_slug)
        if role:
            conditions.append("f.role = %s")
            params.append(role)
        if after_cursor:
            cursor_ts, cursor_id = decode_cursor(after_cursor)
            conditions.append(
                "(f.created_at > %s::timestamptz"
                " OR (f.created_at = %s::timestamptz AND f.id > %s::uuid))"
            )
            params.extend([cursor_ts, cursor_ts, cursor_id])

        fetch_limit = limit + 1

        if aoi_id:
            extra_conditions = (
                (" AND " + " AND ".join(conditions)) if conditions else ""
            )
            sql = f"""
                SELECT
                    f.id, f.type_slug, f.role, f.name,
                    f.attributes,
                    f.overlap_ratio,
                    f.created_at
                FROM serving_geo.feature_aoi_v f
                WHERE f.aoi_id = %s{extra_conditions}
                ORDER BY f.created_at, f.id
                LIMIT %s
            """
            params = [aoi_id] + params + [fetch_limit]
        else:
            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                SELECT
                    f.id, f.type_slug, f.role, f.name,
                    f.attributes,
                    f.created_at
                FROM serving_geo.feature_v f
                {where_clause}
                ORDER BY f.created_at, f.id
                LIMIT %s
            """
            params = params + [fetch_limit]

        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["created_at"], str(last["id"]))

        for row in rows:
            row.pop("created_at", None)

        return {"features": rows, "next_cursor": next_cursor}

    def count_features(self, *, type_slug: str | None = None) -> int:
        with self.connection.cursor() as cur:
            if type_slug is None:
                cur.execute("SELECT COUNT(*) FROM serving_geo.feature_v")
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM serving_geo.feature_v WHERE type_slug = %s",
                    (type_slug,),
                )
            return int(cur.fetchone()[0])

    def count_features_within_polygon(
        self,
        *,
        aoi_id: str,
        polygon_geojson: str,
        type_slugs: list[str],
    ) -> dict[str, int]:
        # Centroid-based containment query (see migration r027).
        # ``ST_Covers(polygon, geometry_centroid)`` against the
        # GiST-indexed ``geometry_centroid`` column is 5-10× faster
        # than the polygon-vs-polygon intersect it replaced because:
        #   - the index returns exactly the rows whose centroid bbox
        #     is in the polygon's bbox (no refilter pass on full
        #     polygons), and
        #   - the per-row test is point-in-polygon, not polygon-vs-
        #     polygon.
        #
        # ``ST_Covers`` (not ``ST_Contains``) on purpose: ST_Contains
        # uses DE-9IM exclusive-boundary semantics — a centroid that
        # lands EXACTLY on the polygon boundary is excluded. For
        # grid-aligned manzanas whose ``ST_PointOnSurface`` can fall
        # on a shared block edge, that produces silent undercount. The
        # client-side ray-cast PIP in selection-filter.ts is also
        # ambiguous at the boundary, so the two implementations could
        # disagree on the same polygon. ST_Covers is the standard
        # PostGIS recommendation for inclusive containment.
        #
        # Trade-off: features that straddle the boundary count if
        # their centroid is inside or on the boundary, miss otherwise.
        # Same approximation the legacy client-side path used and the
        # same one humans intuit as "did the circle catch this?".
        #
        # Bounded by aoi_id so a Mexicali selection doesn't
        # accidentally walk CDMX features. type_slug = ANY(%s) lets
        # the caller pin the query to the layers actually visible in
        # the panel.
        if not type_slugs:
            return {}
        sql = """
            SELECT type_slug, COUNT(*)::bigint AS n
            FROM serving_geo.feature_aoi_v
            WHERE aoi_id = %s
              AND type_slug = ANY(%s)
              AND ST_Covers(
                  ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                  geometry_centroid
              )
            GROUP BY type_slug
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (aoi_id, list(type_slugs), polygon_geojson))
            return {row[0]: int(row[1]) for row in cur.fetchall()}


class PostgresExploreReader:
    def __init__(self, connection):
        self.connection = connection

    def _read_query(self, sql: str) -> list[dict]:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]

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
            ORDER BY stat_scope, row_count DESC, type_slug NULLS FIRST, role NULLS FIRST
            """
        )

    def list_executor_health(self) -> list[dict]:
        return self._read_query(
            """
            SELECT *
            FROM explore.ops_executor_health_v
            ORDER BY heartbeat_at DESC, executor_id
            """
        )


class PostgresTileSnapshotRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.tile_snapshot_registry
                  (layer_id, artifact_path, artifact_uri, snapshot_version, source_relation, source_lineage_ref, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    layer_id,
                    artifact_path,
                    artifact_uri,
                    snapshot_version,
                    source_relation,
                    json.dumps(source_lineage_ref or {}),
                    status,
                ),
            )
            return str(cur.fetchone()[0])

    def get_latest_snapshot(self, *, layer_id: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT
                  id::text AS id,
                  layer_id,
                  artifact_path,
                  artifact_uri,
                  snapshot_version,
                  source_relation,
                  source_lineage_ref,
                  status,
                  created_at
                FROM curated_geo.tile_snapshot_registry
                WHERE layer_id = %s
                ORDER BY created_at DESC, snapshot_version DESC
                LIMIT 1
                """,
                (layer_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def list_snapshots(self, *, layer_id: str | None = None) -> list[dict]:
        query = """
            SELECT
              id::text AS id,
              layer_id,
              artifact_path,
              artifact_uri,
              snapshot_version,
              source_relation,
              source_lineage_ref,
              status,
              created_at
            FROM curated_geo.tile_snapshot_registry
        """
        params: list[str] = []
        if layer_id:
            query += " WHERE layer_id = %s"
            params.append(layer_id)
        query += " ORDER BY created_at DESC, snapshot_version DESC"
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


class PostgresAOISummaryRepository:
    """Upsert + read helpers for curated_geo.aoi_indicator_summary.

    Uses the two partial unique indexes defined in r015 so that base-city
    and scenario-scoped rows coexist without NULL-scenario duplication.
    """

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
        # Partial unique indexes cannot be referenced by name in ON CONFLICT;
        # use the inference form (column list + WHERE predicate) instead.
        with self.connection.cursor() as cur:
            if scenario_id is None:
                cur.execute(
                    """
                    INSERT INTO curated_geo.aoi_indicator_summary
                      (aoi_id, scenario_id, indicator_id, value, aggregation_note, status)
                    VALUES (%s, NULL, %s, %s, %s, %s)
                    ON CONFLICT (aoi_id, indicator_id) WHERE scenario_id IS NULL
                    DO UPDATE SET
                      value            = EXCLUDED.value,
                      aggregation_note = EXCLUDED.aggregation_note,
                      status           = EXCLUDED.status,
                      computed_at      = now()
                    RETURNING id
                    """,
                    (aoi_id, indicator_id, value, aggregation_note, status),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO curated_geo.aoi_indicator_summary
                      (aoi_id, scenario_id, indicator_id, value, aggregation_note, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (aoi_id, scenario_id, indicator_id)
                      WHERE scenario_id IS NOT NULL
                    DO UPDATE SET
                      value            = EXCLUDED.value,
                      aggregation_note = EXCLUDED.aggregation_note,
                      status           = EXCLUDED.status,
                      computed_at      = now()
                    RETURNING id
                    """,
                    (
                        aoi_id,
                        scenario_id,
                        indicator_id,
                        value,
                        aggregation_note,
                        status,
                    ),
                )
            return str(cur.fetchone()[0])

    def get_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        scenario_id: str | None = None,
    ) -> dict | None:
        scenario_clause, scenario_params = _scenario_filter("scenario_id", scenario_id)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT id, aoi_id, scenario_id, indicator_id, value,
                       aggregation_note, status, computed_at
                FROM curated_geo.aoi_indicator_summary
                WHERE aoi_id = %s AND {scenario_clause} AND indicator_id = %s
                """,
                [aoi_id, *scenario_params, indicator_id],
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_summaries(
        self,
        *,
        aoi_id: str,
        scenario_id: str | None = None,
        only_current: bool = True,
    ) -> list[dict]:
        conditions = ["aoi_id = %s"]
        params: list = [aoi_id]
        clause, extra = _scenario_filter("scenario_id", scenario_id)
        conditions.append(clause)
        params.extend(extra)
        if only_current:
            conditions.append("status = 'current'")

        where_clause = " AND ".join(conditions)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT id, aoi_id, scenario_id, indicator_id, value,
                       aggregation_note, status, computed_at
                FROM curated_geo.aoi_indicator_summary
                WHERE {where_clause}
                ORDER BY computed_at DESC
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def fetch_aoi_children(
        self,
        *,
        aoi_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> list[tuple[float | None, float, str]]:
        # Weight = overlap_measure (absolute area inside the AOI); see
        # Protocol docstring for why overlap_ratio is wrong here.
        scenario_clause, scenario_params = _scenario_overlay("ir", scenario_id)
        params: list = [indicator_slug, *scenario_params, aoi_id]
        with self.connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT ir.value_numeric,
                       af.overlap_measure,
                       ir.quality_flag
                FROM curated_geo.aoi_feature af
                LEFT JOIN curated_geo.indicator_result ir
                     ON ir.feature_id = af.feature_id
                    AND ir.indicator_slug = %s
                    AND ir.status = 'current'
                    AND {scenario_clause}
                WHERE af.aoi_id = %s
                """,
                params,
            )
            return [
                (
                    float(value) if value is not None else None,
                    float(weight),
                    quality or "missing",
                )
                for value, weight, quality in cur.fetchall()
            ]


# ─── Job run ────────────────────────────────────────────────────────────────


class PostgresJobRunRepository:
    def __init__(self, connection):
        self.connection = connection

    def start_job(
        self,
        *,
        model_family: str,
        code_version: str,
        scenario_id: str | None = None,
        status: str = "queued",
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str:
        # ⚠ clock_timestamp(), NOT now(). A unit opens its job_run row, runs its
        # compute, and completes the row all on one connection inside ONE
        # transaction — and now() is transaction_timestamp(), constant for that
        # whole transaction. With now() on both ends, every finished unit
        # recorded completed_at == started_at, so `sum(completed_at -
        # started_at)` — the runner-time query the ops-plane measurement spec is
        # written around — returned 00:00:00 on every row ever written.
        # clock_timestamp() is the wall clock at statement time, which is what
        # "when did this unit start" actually means. The recovered-provenance
        # path supplies its original start for both queued_at and started_at;
        # otherwise queued_at is stamped here. Both database ordering checks
        # therefore remain true.
        started_at_sql = (
            "%s"
            if started_at is not None
            else "clock_timestamp()"
            if status == "running"
            else "NULL"
        )
        queued_at_sql = "%s" if started_at is not None else "clock_timestamp()"
        with self.connection.cursor() as cur:
            # pg_backend_pid() is evaluated on THIS connection, which is the one
            # that will run the compute — that is the whole point. Capturing it
            # anywhere else would record a pid nobody needs to cancel.
            cur.execute(
                f"""
                INSERT INTO jobs_runtime.job_run
                  (scenario_id, model_family, code_version, status, queued_at, started_at,
                   run_id, backend_pid)
                VALUES (%s, %s, %s, %s, {queued_at_sql}, {started_at_sql}, %s, pg_backend_pid())
                RETURNING id::text
                """,
                (
                    (
                        scenario_id,
                        model_family,
                        code_version,
                        status,
                        started_at,
                        started_at,
                        run_id,
                    )
                    if started_at is not None
                    else (scenario_id, model_family, code_version, status, run_id)
                ),
            )
            return str(cur.fetchone()[0])

    def cancel_run_backends(self, run_id: str) -> int:
        """Cancel the running query of every live backend for ``run_id``.

        The only thing that actually stops in-flight compute. An engine's own
        cancel is cooperative and lands at the next step boundary, which a
        minutes-long PostGIS statement does not reach — so without this, "cancel"
        means "stop scheduling more work" and the current unit runs to
        completion regardless.

        ``pg_cancel_backend`` cancels the statement, not the session: the
        runner's psycopg call raises ``QueryCanceled`` and its own error
        handling takes over. Returns how many backends were signalled; a pid may
        already be gone, which Postgres reports as False rather than an error.
        """
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE pg_cancel_backend(backend_pid))
                FROM jobs_runtime.job_run
                WHERE run_id = %s
                  AND backend_pid IS NOT NULL
                  AND status NOT IN ('succeeded', 'failed', 'cancelled')
                """,
                (run_id,),
            )
            return int(cur.fetchone()[0])

    def complete_job(
        self,
        job_run_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs_runtime.job_run
                SET status = %s,
                    completed_at = clock_timestamp(),
                    error_message = %s,
                    started_at = COALESCE(started_at, clock_timestamp())
                WHERE id = %s
                """,
                (status, error_message, job_run_id),
            )


# ─── Indicator catalog ──────────────────────────────────────────────────────


class PostgresIndicatorCatalogRepository:
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
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.indicator_catalog
                  (slug, name, description, model_family, computation_kind, value_type,
                   unit, valid_min, valid_max, aggregation_method, applies_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    name               = EXCLUDED.name,
                    description        = EXCLUDED.description,
                    model_family       = EXCLUDED.model_family,
                    computation_kind   = EXCLUDED.computation_kind,
                    value_type         = EXCLUDED.value_type,
                    unit               = EXCLUDED.unit,
                    valid_min          = EXCLUDED.valid_min,
                    valid_max          = EXCLUDED.valid_max,
                    aggregation_method = EXCLUDED.aggregation_method,
                    applies_to         = EXCLUDED.applies_to
                RETURNING id::text
                """,
                (
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
                ),
            )
            return str(cur.fetchone()[0])

    def get_indicator(self, slug: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id::text, slug, name, description, model_family, computation_kind,
                       value_type, unit, valid_min, valid_max, aggregation_method, applies_to
                FROM curated_geo.indicator_catalog WHERE slug = %s
                """,
                (slug,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_indicators(self, *, with_values_for_aoi: str | None = None) -> list[dict]:
        """Catalog rows, optionally only those with current values in an AOI.

        The catalog says what an indicator *is*, not whether anyone has
        computed it. A registered-but-uncomputed indicator is a real state —
        `urban_form_block_planned_floors` sat in the catalog with zero rows
        until the `floors` attribute was seeded — and a caller that offers
        every catalog row to a user offers dead options that render an empty
        map, which reads as a broken indicator rather than an absent one.

        ``with_values_for_aoi`` narrows to indicators holding at least one
        ``status='current'`` baseline result on a feature belonging to that
        AOI. Scenario rows are excluded (``scenario_id IS NULL``): a value
        that exists only inside a what-if is not a reason to offer the
        indicator on the baseline map.

        ``EXISTS`` rather than a join, for the same reason the tile functions
        use it — one row per indicator regardless of how many results match.
        An unparseable uuid yields an empty list rather than an error, matching
        the fail-closed direction r039 chose for the tile filter.
        """
        clause = ""
        params: list = []
        if with_values_for_aoi is not None:
            clause = """
                WHERE EXISTS (
                    SELECT 1
                    FROM curated_geo.indicator_result ir
                    JOIN curated_geo.aoi_feature af ON af.feature_id = ir.feature_id
                    WHERE ir.indicator_slug = c.slug
                      AND ir.status = 'current'
                      AND ir.scenario_id IS NULL
                      AND ir.value_numeric IS NOT NULL
                      AND af.aoi_id = %s::uuid
                )
            """
            params.append(with_values_for_aoi)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT c.id::text, c.slug, c.name, c.description, c.model_family,
                       c.computation_kind, c.value_type, c.unit, c.valid_min,
                       c.valid_max, c.aggregation_method, c.applies_to
                FROM curated_geo.indicator_catalog c
                {clause}
                ORDER BY c.slug
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def set_dependencies(
        self,
        *,
        indicator_slug: str,
        depends_on: list[tuple[str, float | None]],
    ) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM curated_geo.indicator_catalog WHERE slug = %s",
                (indicator_slug,),
            )
            parent = cur.fetchone()
            if parent is None:
                raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
            parent_id = parent[0]

            cur.execute(
                "DELETE FROM curated_geo.indicator_dependency WHERE indicator_id = %s",
                (parent_id,),
            )
            for dep_slug, weight in depends_on:
                cur.execute(
                    "SELECT id FROM curated_geo.indicator_catalog WHERE slug = %s",
                    (dep_slug,),
                )
                child = cur.fetchone()
                if child is None:
                    raise ValueError(f"Unknown dependency slug: {dep_slug!r}")
                cur.execute(
                    """
                    INSERT INTO curated_geo.indicator_dependency
                      (indicator_id, depends_on_id, weight)
                    VALUES (%s, %s, %s)
                    """,
                    (parent_id, child[0], weight),
                )

    def list_dependencies(self) -> list[dict]:
        """Every composite → input edge, as slugs and weights.

        The write side of `indicator_dependency` has existed since r016 and
        nothing has ever read it back, so a composite's inputs were only
        visible by inspecting the Python registry. Returning the whole edge
        set in one query — there are single digits of them — lets a caller
        build both directions of the graph without a query per indicator.
        """
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT parent.slug        AS indicator_slug,
                       parent.name        AS indicator_name,
                       child.slug         AS depends_on_slug,
                       child.name         AS depends_on_name,
                       child.unit         AS depends_on_unit,
                       d.weight
                FROM curated_geo.indicator_dependency d
                JOIN curated_geo.indicator_catalog parent ON parent.id = d.indicator_id
                JOIN curated_geo.indicator_catalog child  ON child.id = d.depends_on_id
                ORDER BY parent.slug, child.slug
                """
            )
            return [dict(row) for row in cur.fetchall()]


# ─── Asset graph ────────────────────────────────────────────────────────────


class PostgresAssetGraphRepository:
    """Reads the r041 asset graph through its serving views.

    Every query here is graph-sized — 25 assets and 40 edges in the shipped
    catalog — so none of them paginate and `lineage` returns a whole closure
    in one round trip rather than a query per node.
    """

    def __init__(self, connection):
        self.connection = connection

    def get_asset(self, asset_key: str) -> dict | None:
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT asset_key, kind, backing_kind, backing_ref,
                       version_strategy, code_version, partition_dimensions,
                       feature_type_context, feature_type_slug, description
                FROM explore.asset_v
                WHERE asset_key = %s
                """,
                (asset_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def lineage(
        self, *, asset_key: str, direction: str, max_depth: int | None = None
    ) -> list[dict]:
        """The transitive closure around `asset_key`.

        `asset_lineage_v` emits both directions from one recursion, so
        "what does this need" and "what breaks if this changes" cannot drift
        apart into two implementations that disagree.
        """
        if direction not in {"upstream", "downstream"}:
            raise ValueError(f"direction must be upstream or downstream: {direction!r}")
        clauses = ["root_key = %s", "direction = %s"]
        params: list = [asset_key, direction]
        if max_depth is not None:
            clauses.append("depth <= %s")
            params.append(max_depth)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT related_key, related_kind, depth
                FROM serving_geo.asset_lineage_v
                WHERE {" AND ".join(clauses)}
                ORDER BY depth, related_key
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    def freshness(self, *, asset_keys: list[str] | None = None) -> list[dict]:
        """Freshness per asset and partition.

        `state='absent'` means no materialization has been recorded, which is
        the state of every derived asset until the compute path writes one.
        It is reported rather than smoothed over: an absent version is a real
        answer, and hiding it behind "unknown" would make a graph that has
        never run look the same as one that is current.
        """
        clause = ""
        params: list = []
        if asset_keys is not None:
            if not asset_keys:
                return []
            clause = "WHERE asset_key = ANY(%s)"
            params.append(asset_keys)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT asset_key, kind, partition_key, state, expected_version,
                       materialized_version, coverage_complete, materialized_at
                FROM explore.asset_freshness_v
                {clause}
                ORDER BY asset_key, partition_key
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


# ─── Indicator result ───────────────────────────────────────────────────────


class PostgresIndicatorResultRepository:
    """Writer uses the Postgres trigger for business-rule enforcement."""

    def __init__(self, connection):
        self.connection = connection

    def _mark_existing_current_stale(
        self,
        cur,
        *,
        feature_id: str,
        indicator_id: str,
        scenario_id: str | None,
    ) -> None:
        scenario_clause, scenario_params = _scenario_filter("scenario_id", scenario_id)
        cur.execute(
            f"""
            UPDATE curated_geo.indicator_result
            SET status = 'stale', updated_at = now()
            WHERE feature_id = %s AND {scenario_clause}
              AND indicator_id = %s AND status = 'current'
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
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM curated_geo.indicator_catalog WHERE slug = %s",
                (indicator_slug,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
            indicator_id = row[0]

            self._mark_existing_current_stale(
                cur,
                feature_id=feature_id,
                indicator_id=str(indicator_id),
                scenario_id=scenario_id,
            )

            value_json_serialised = (
                psycopg2.extras.Json(value_json) if value_json is not None else None
            )

            cur.execute(
                """
                INSERT INTO curated_geo.indicator_result
                  (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
                   job_run_id, model_version, value_numeric, value_text, value_json,
                   quality_flag, confidence, missing_reason, status)
                VALUES (%s, '', %s, %s, '', %s, %s, %s, %s, %s, %s, %s, %s, 'current')
                RETURNING id::text
                """,
                (
                    feature_id,
                    scenario_id,
                    indicator_id,
                    job_run_id,
                    model_version,
                    value_numeric,
                    value_text,
                    value_json_serialised,
                    quality_flag,
                    confidence,
                    missing_reason,
                ),
            )
            return str(cur.fetchone()[0])

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
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM curated_geo.indicator_catalog WHERE slug = %s",
                (indicator_slug,),
            )
            found = cur.fetchone()
            if found is None:
                raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
            indicator_id = str(found[0])

            feature_ids = [fid for fid, _ in rows]
            scenario_clause, scenario_params = _scenario_filter(
                "scenario_id", scenario_id
            )
            cur.execute(
                f"""
                UPDATE curated_geo.indicator_result
                SET status = 'stale', updated_at = now()
                WHERE feature_id = ANY(%s::uuid[])
                  AND {scenario_clause}
                  AND indicator_id = %s
                  AND status = 'current'
                """,
                [feature_ids, *scenario_params, indicator_id],
            )

            psycopg2.extras.execute_batch(
                cur,
                """
                INSERT INTO curated_geo.indicator_result
                  (feature_id, feature_type, scenario_id, indicator_id, indicator_slug,
                   job_run_id, value_numeric, quality_flag, status)
                VALUES (%s, '', %s, %s, '', %s, %s, %s, 'current')
                """,
                [
                    (fid, scenario_id, indicator_id, job_run_id, value, quality_flag)
                    for fid, value in rows
                ],
                page_size=500,
            )
            return len(rows)

    def get_current(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> dict | None:
        scenario_clause, scenario_params = _scenario_overlay("civ", scenario_id)
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT civ.* FROM serving_geo.current_indicator_v civ
                WHERE civ.feature_id = %s AND {scenario_clause}
                  AND civ.indicator_slug = %s
                """,
                [feature_id, *scenario_params, indicator_slug],
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_current_for_indicator(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        scenario_clause, scenario_params = _scenario_overlay("civ", scenario_id)
        limit_sql = "" if limit is None else "LIMIT %s"
        limit_params: list = [] if limit is None else [limit]
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT civ.* FROM serving_geo.current_indicator_v civ
                WHERE civ.indicator_slug = %s AND {scenario_clause}
                {limit_sql}
                """,
                [indicator_slug, *scenario_params, *limit_params],
            )
            return [dict(row) for row in cur.fetchall()]

    def list_current_with_geometry(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        limit: int = 100_000,
    ) -> list[dict]:
        """Current indicator rows joined with EPSG:4326 geometry (WKB bytes).

        Backs the GeoJSON / GeoParquet download paths — one query, no
        extra round-trip for geometry. Feature-type filter is optional;
        omitting it returns every grain the indicator applies to.
        """
        # Build the predicate progressively so NULL-safe scenario matching
        # and the optional feature_type filter compose cleanly.
        clauses = ["ir.indicator_slug = %s", "ir.status = 'current'"]
        params: list = [indicator_slug]
        clause, extra = _scenario_overlay("ir", scenario_id)
        clauses.append(clause)
        params.extend(extra)
        if feature_type is not None:
            clauses.append("ir.feature_type = %s")
            params.append(feature_type)
        params.append(limit)

        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                f"""
                SELECT ir.feature_id::text       AS feature_id,
                       ir.feature_type           AS type_slug,
                       ST_AsBinary(f.geometry)   AS geometry_wkb,
                       ir.value_numeric          AS value_numeric,
                       ir.quality_flag           AS quality_flag,
                       ir.computed_at            AS computed_at
                FROM curated_geo.indicator_result ir
                JOIN curated_geo.feature f ON f.id = ir.feature_id
                WHERE {" AND ".join(clauses)}
                ORDER BY ir.feature_id
                LIMIT %s
                """,
                params,
            )
            return [
                {**row, "geometry_wkb": bytes(row["geometry_wkb"])}
                for row in cur.fetchall()
            ]

    def value_stats(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        aoi_id: str | None = None,
        bins: int = 0,
    ) -> dict:
        # Aliased so the optional aoi_id EXISTS subquery can reference
        # the row's feature_id column without column-name shadowing.
        clauses = [
            "ir.indicator_slug = %s",
            "ir.status = 'current'",
            "ir.value_numeric IS NOT NULL",
        ]
        params: list = [indicator_slug]
        clause, extra = _scenario_overlay("ir", scenario_id)
        clauses.append(clause)
        params.extend(extra)
        if feature_type is not None:
            clauses.append("ir.feature_type = %s")
            params.append(feature_type)
        if aoi_id is not None:
            # EXISTS over JOIN: same shape as the tile functions in
            # r022 / r023, keeps cardinality stable when a feature
            # belongs to multiple AOIs.
            clauses.append(
                "EXISTS (SELECT 1 FROM curated_geo.aoi_feature af "
                "WHERE af.feature_id = ir.feature_id AND af.aoi_id = %s::uuid)"
            )
            params.append(aoi_id)
        where = " AND ".join(clauses)
        with self.connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  COUNT(*)::bigint                                               AS count,
                  MIN(ir.value_numeric)                                          AS min,
                  percentile_cont(0.05) WITHIN GROUP (ORDER BY ir.value_numeric) AS p05,
                  percentile_cont(0.50) WITHIN GROUP (ORDER BY ir.value_numeric) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY ir.value_numeric) AS p95,
                  MAX(ir.value_numeric)                                          AS max
                FROM curated_geo.indicator_result ir
                WHERE {where}
                """,
                params,
            )
            count, vmin, p05, p50, p95, vmax = cur.fetchone()
        stats: dict[str, Any] = {
            "count": int(count),
            "min": float(vmin) if vmin is not None else None,
            "p05": float(p05) if p05 is not None else None,
            "p50": float(p50) if p50 is not None else None,
            "p95": float(p95) if p95 is not None else None,
            "max": float(vmax) if vmax is not None else None,
        }
        if bins > 0:
            stats["histogram"] = self._value_histogram(
                where=where, params=params, bins=bins, stats=stats
            )
        return stats

    def _value_histogram(
        self, *, where: str, params: list, bins: int, stats: dict
    ) -> dict | None:
        """Counts per equal-width bin over the same population as ``stats``.

        **Over the full observed range, not the ramp's p05–p95.** The ramp
        clips outliers so one extreme value cannot flatten the colour scale;
        the histogram's job is the opposite — to show that the outlier is
        there. The client draws the ramp's breaks *on top* of this, so the
        two together say "here is the whole distribution, and here is the
        part the colours resolve".

        Returns ``None`` when there is nothing to bin. Two degenerate cases
        are handled rather than left to Postgres:

        - **No rows.** ``width_bucket`` would have no bounds to work with.
        - **Every value identical** (``min == max``), which includes the very
          common single-feature case. ``width_bucket`` requires
          ``lo < hi`` and raises otherwise, so this returns one full bin
          instead — which is also the honest picture.
        """
        count = stats["count"]
        lo = stats["min"]
        hi = stats["max"]
        if not count or lo is None or hi is None:
            return None
        if hi <= lo:
            return {"bins": 1, "lo": lo, "hi": hi, "width": 0.0, "counts": [count]}

        with self.connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT bucket, COUNT(*)::bigint
                FROM (
                  SELECT
                    -- `width_bucket` puts a value equal to `hi` in bucket
                    -- n+1, which is an overflow bucket rather than a bin.
                    -- `LEAST` folds it back into the last real bin, so the
                    -- maximum lands inside the histogram instead of beside
                    -- it. Without this the tallest-value bar is always
                    -- missing exactly the maximum.
                    LEAST(
                      width_bucket(ir.value_numeric, %s, %s, %s), %s
                    ) AS bucket
                  FROM curated_geo.indicator_result ir
                  WHERE {where}
                ) b
                GROUP BY bucket
                """,
                [lo, hi, bins, bins, *params],
            )
            found = {int(bucket): int(n) for bucket, n in cur.fetchall()}

        counts = [found.get(i, 0) for i in range(1, bins + 1)]
        return {
            "bins": bins,
            "lo": lo,
            "hi": hi,
            "width": (hi - lo) / bins,
            "counts": counts,
        }


class PostgresRunPlanRepository:
    """The resolved feature set for a run, stored once and read by handle.

    Both ops planes used to carry this inside the workflow input — one uuid per
    feature per dirty indicator, 258,675 of them for a full CDMX recompute, which
    the Hatchet track cannot even submit. Here it costs three rows and the engine
    carries a ``plan_id``.

    ⚠ Write-once. Nothing updates a plan: replay, retry and crash recovery must
    all see the set the submitter resolved, which is the whole reason this is a
    table rather than a query the unit re-runs for itself.
    """

    def __init__(self, connection):
        self.connection = connection

    def write(self, *, plan_id: str, units: dict[str, list[str]]) -> None:
        """Persist one row per dirty indicator. Call once, before submitting.

        ⚠ The caller must COMMIT before handing ``plan_id`` to an engine. A
        worker can dequeue immediately and will read an uncommitted plan as a
        missing one — which `features_for` reports as a lost write, on a run that
        was perfectly valid.

        ⚠ Explicit ``::uuid`` / ``::uuid[]`` casts. psycopg2 adapts a Python list
        of strings to a *text* array, and Postgres will not implicitly cast
        ``text[]`` to ``uuid[]``; without the cast this fails at insert.

        Sorted by slug so two identical plans produce byte-identical statements —
        the fingerprint that feeds run deduplication is computed from the same
        content, and an unstable ordering there would silently defeat it.
        """
        if not units:
            raise ValueError("refusing to write an empty run plan")
        with self.connection.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO jobs_runtime.run_plan "
                "(plan_id, indicator_slug, feature_ids) VALUES %s",
                [
                    (plan_id, slug, [str(f) for f in features])
                    for slug, features in sorted(units.items())
                ],
                template="(%s::uuid, %s, %s::uuid[])",
            )

    def features_for(self, *, plan_id: str, indicator_slug: str) -> list[str]:
        """The features this unit must compute.

        ⚠ ``feature_ids::text[]``, not ``feature_ids``. psycopg2 has no typecaster
        for ``uuid[]`` unless ``register_uuid`` has been called globally, so the
        column comes back as the raw array *literal* — the string ``'{a,b,c}'``,
        which iterates as characters and yields 180,001 one-character "features"
        for a 5,000-feature unit. Casting in SQL keeps the fix local; registering
        a global adapter would change how every other query in the process
        decodes uuids.

        ⚠ Raises rather than returning ``[]`` when the row is absent. An empty
        feature set is indistinguishable from a successful no-op: the unit would
        write nothing, report success, and leave the indicator stale with no
        error anywhere. A missing plan row is always a defect — a lost write, an
        uncommitted transaction, or a worker pointed at the wrong database — and
        has to fail loudly enough to be seen.
        """
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT feature_ids::text[] FROM jobs_runtime.run_plan "
                "WHERE plan_id = %s::uuid AND indicator_slug = %s",
                (plan_id, indicator_slug),
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(
                f"no run_plan row for plan {plan_id!r} / {indicator_slug!r} — "
                "the plan was never written, was not committed before the run "
                "was submitted, or this connection is on a different database"
            )
        return [str(feature_id) for feature_id in row[0]]

    def delete(self, *, plan_id: str) -> int:
        """Reclaim a plan whose run has reached a terminal state.

        Best-effort by design: the sweep below is what guarantees the table
        stays bounded. A run that crashed, was cancelled before its finalizer,
        or lost its idempotency race never reaches here.
        """
        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.run_plan WHERE plan_id = %s::uuid",
                (plan_id,),
            )
            return cur.rowcount

    def sweep(self, *, older_than_hours: int) -> int:
        """Reclaim orphans — plans no terminal run will ever come back for.

        Three ways a plan is abandoned, none of which reach `delete`: a submit
        that crashed between writing the plan and enqueuing the work; a run
        cancelled before its finalizer ran; and an idempotency collision, where
        the engine hands back an existing run and the plan just written is
        never referenced by anything.

        ⚠ ``older_than_hours`` must exceed the longest run this deployment can
        produce. Sweeping a plan out from under a live run makes every
        subsequent unit fail with a missing-plan LookupError — the loud failure
        that is supposed to mean "a write was lost". The unit timeout alone is
        120 minutes by default, so the margin here is deliberately generous.
        """
        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.run_plan "
                "WHERE created_at < now() - make_interval(hours => %s)",
                (older_than_hours,),
            )
            return cur.rowcount


class PostgresOpsRunReader:
    """Read side of the run envelope, for the Ops dashboard.

    Every query here is polled every two seconds by every open dashboard tab, so
    the shapes are chosen for that cadence rather than for brevity.
    """

    def __init__(self, connection):
        self.connection = connection

    def _read_query(self, sql: str, params: tuple | dict | None = None) -> list[dict]:
        """Own helper, not ``PostgresExploreReader``'s — that one takes no params."""
        with self.connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    # ── Runs ────────────────────────────────────────────────────────────────

    _UNIT_COUNTS = """
        LEFT JOIN LATERAL (
            SELECT count(u.indicator_slug)                                AS units_total,
                   count(*) FILTER (WHERE u.status = ANY(%(terminal)s))   AS units_terminal,
                   count(*) FILTER (WHERE u.status = 'succeeded')         AS units_succeeded,
                   count(*) FILTER (WHERE u.status = 'failed')            AS units_failed,
                   count(*) FILTER (WHERE u.status = 'running')           AS units_running,
                   max(u.completed_at)                                    AS units_last_terminal_at
            FROM jobs_runtime.job_unit u
            WHERE u.run_id = r.id
        ) c ON TRUE
    """
    """Counts for one run, applied *after* the run set is limited.

    ⚠ ``count(u.indicator_slug)``, not ``count(*)``: a bare ``count(*)`` in a
    correlated subquery over zero rows still returns 1 for the aggregate row.

    ⚠ The terminal list is bound, not written out — ``port.UNIT_TERMINAL_STATES``
    is the one definition, and ``max(completed_at)`` is only meaningful because
    ``set_unit_status`` stamps it for every member of that set, skipped and
    cancelled included.
    """

    def list_runs(
        self,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest runs first, each with its unit counts.

        ⚠ The runs are limited in a CTE and the counts joined onto that. Joining
        ``job_unit`` first and grouping after — the obvious shape — makes Postgres
        aggregate every ``job_unit`` row ever written before ``LIMIT`` applies.

        ⚠ ``statuses=[]`` must not reach the bind. ``%(s)s IS NULL OR x = ANY(%(s)s)``
        is true for a NULL bind and false for an empty array, so an empty list
        would silently mean "no runs" where the caller meant "no filter". The
        route normalises, and so does this, because both are one expression and
        the failure is invisible.
        """
        from reimagina.orchestration.port import UNIT_TERMINAL_STATES

        return self._read_query(
            f"""
            WITH page AS (
                SELECT r.id, r.scenario_id, r.aoi_id, r.status,
                       r.code_version, r.submitted_at, r.completed_at
                FROM jobs_runtime.orchestration_run r
                WHERE (%(statuses)s IS NULL OR r.status = ANY(%(statuses)s))
                ORDER BY r.submitted_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
            )
            SELECT r.id::text        AS run_id,
                   r.status,
                   r.scenario_id::text AS scenario_id,
                   r.aoi_id,
                   r.code_version,
                   r.submitted_at,
                   r.completed_at,
                   c.units_total, c.units_terminal, c.units_succeeded,
                   c.units_failed, c.units_running, c.units_last_terminal_at
            FROM page r
            {self._UNIT_COUNTS}
            ORDER BY r.submitted_at DESC
            """,
            {
                "statuses": list(statuses) if statuses else None,
                "terminal": sorted(UNIT_TERMINAL_STATES),
                "limit": limit,
                "offset": offset,
            },
        )

    def count_runs(self, *, statuses: list[str] | None = None) -> int:
        rows = self._read_query(
            """
            SELECT count(*) AS total
            FROM jobs_runtime.orchestration_run r
            WHERE (%(statuses)s IS NULL OR r.status = ANY(%(statuses)s))
            """,
            {"statuses": list(statuses) if statuses else None},
        )
        return int(rows[0]["total"])

    def count_runs_by_status(self) -> dict[str, int]:
        """Unfiltered tallies. Statuses with no rows are absent; caller zero-fills."""
        rows = self._read_query(
            """
            SELECT status, count(*) AS n
            FROM jobs_runtime.orchestration_run
            GROUP BY status
            """
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def get_run_row(self, run_id: str) -> dict | None:
        """One run with its counts, or None — including for an id no run can have."""
        from reimagina.orchestration.port import UNIT_TERMINAL_STATES

        if not _is_run_id(run_id):
            return None
        rows = self._read_query(
            f"""
            SELECT r.id::text        AS run_id,
                   r.status,
                   r.scenario_id::text AS scenario_id,
                   r.aoi_id,
                   r.code_version,
                   r.submitted_at,
                   r.completed_at,
                   c.units_total, c.units_terminal, c.units_succeeded,
                   c.units_failed, c.units_running, c.units_last_terminal_at
            FROM jobs_runtime.orchestration_run r
            {self._UNIT_COUNTS}
            WHERE r.id = %(run_id)s::uuid
            """,
            {"run_id": str(run_id), "terminal": sorted(UNIT_TERMINAL_STATES)},
        )
        return rows[0] if rows else None

    def list_units_detailed(self, run_id: str) -> list[dict]:
        """Units with their timings, ordered as the run dispatched them.

        ``COALESCE(started_at, queued_at)`` rather than the slug: the operator is
        reading a timeline, and a unit that never started still has a place in it.
        """
        if not _is_run_id(run_id):
            return []
        return self._read_query(
            """
            SELECT indicator_slug AS indicator,
                   status, detail, queued_at, started_at, completed_at
            FROM jobs_runtime.job_unit
            WHERE run_id = %(run_id)s::uuid
            ORDER BY COALESCE(started_at, queued_at), indicator_slug
            """,
            {"run_id": str(run_id)},
        )

    # ── Deletion ────────────────────────────────────────────────────────────

    def delete_run(self, run_id: str) -> bool:
        """Forget one run. ``job_unit`` goes with it by cascade (r028)."""
        if not _is_run_id(run_id):
            return False
        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run WHERE id = %(run_id)s::uuid",
                {"run_id": str(run_id)},
            )
            return cur.rowcount > 0

    def delete_terminal_runs(self) -> int:
        """Forget every finished run.

        ⚠ Terminal only, and the filter is bound from ``RUN_TERMINAL_STATES``
        rather than written out. A live run's workflow still owns these rows —
        deleting the envelope mid-flight would have ``s_open_run``'s successors
        write into a run that no longer exists, and ``s_finalize`` recreate part
        of it.
        """
        from reimagina.orchestration.port import RUN_TERMINAL_STATES

        with self.connection.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run "
                "WHERE status = ANY(%(terminal)s)",
                {"terminal": sorted(RUN_TERMINAL_STATES)},
            )
            return cur.rowcount

    # ── Executors ───────────────────────────────────────────────────────────

    def list_executor_health(self) -> list[dict]:
        """Live executors, heartbeat age already in seconds.

        ⚠ ``clock_timestamp()``, not ``now()``: this runs inside the API's pooled
        request transaction, where ``now()`` is the transaction's start time.

        ⚠ The base table, not ``explore.ops_executor_health_v``. ``explore`` is a
        human-facing contract rather than an application dependency (AGENTS.md),
        and the view emits ``heartbeat_age`` as an ``interval`` — which a Pydantic
        float field rejects outright, as a 500.
        """
        return self._read_query(
            """
            SELECT executor_id,
                   role,
                   app_version,
                   started_at,
                   heartbeat_at,
                   EXTRACT(EPOCH FROM (clock_timestamp() - heartbeat_at))::float
                       AS heartbeat_age_s
            FROM jobs_runtime.ops_executor_heartbeat
            ORDER BY heartbeat_at DESC, executor_id
            """
        )
