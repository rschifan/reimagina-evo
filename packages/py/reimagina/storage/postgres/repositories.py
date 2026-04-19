"""PostgreSQL/PostGIS repository implementations."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import psycopg2.extras
import pyarrow.parquet as pq

from .._utils import normalize_json_value as _normalize_json_value


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
                (name, provider, storage_type, integration_role, storage_uri, version, license, notes),
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
                (source_id, orchestrator, orchestrator_run_id, transformation_step, code_version),
            )
            return str(cur.fetchone()[0])

    def complete_ingestion_run(self, run_id: str, *, status: str, details: dict | None = None) -> None:
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
                    str(row[parent_id_col]) if parent_id_col and parent_id_col in row.index else None,
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
            if optional and optional in available_columns and optional not in requested_columns:
                requested_columns.append(optional)

        batch_size = 10_000
        inserted_count = 0
        for batch in parquet_file.iter_batches(batch_size=batch_size, columns=requested_columns):
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
                        str(record[parent_id_col]) if parent_id_col and record.get(parent_id_col) else None,
                        str(record[name_col]) if name_col and record.get(name_col) is not None else None,
                        psycopg2.Binary(record["geometry"]) if record.get("geometry") is not None else None,
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

    def bulk_upsert_current(self, records: list[dict], *, source_id: str, ingestion_run_id: str) -> int:
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
            insert_rows.append((
                r["feature_id"],
                r["attribute_key"],
                r["value_type"],
                value_numeric,
                value_text,
                value_json,
                source_id,
                ingestion_run_id,
                json.dumps(r["lineage_ref"]) if r.get("lineage_ref") else None,
            ))

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
                template=(
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, true)"
                ),
                page_size=len(insert_rows),
            )
        return len(records)


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


class PostgresAOIRepository:
    def __init__(self, connection):
        self.connection = connection

    def create_aoi(self, *, project_id: str, name: str, geometry_wkt: str, description: str | None = None) -> str:
        with self.connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.area_of_interest
                  (project_id, name, description, geometry)
                VALUES (%s, %s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326))
                RETURNING id
                """,
                (project_id, name, description, geometry_wkt),
            )
            return str(cur.fetchone()[0])

    def compute_overlaps(self, aoi_id: str) -> int:
        with self.connection.cursor() as cur:
            cur.execute("DELETE FROM curated_geo.aoi_feature WHERE aoi_id = %s", (aoi_id,))
            cur.execute(
                """
                WITH aoi_raw AS (
                  SELECT geometry
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
                WHERE ST_Area(i.overlap_geometry::geography) > 0
                """,
                (aoi_id, aoi_id),
            )
            poly_count = cur.rowcount
            cur.execute(
                """
                WITH aoi_raw AS (
                  SELECT geometry
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
                WHERE ST_Length(i.overlap_geometry::geography) > 0
                """,
                (aoi_id, aoi_id),
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
                """,
                (aoi_id, aoi_id),
            )
            point_count = cur.rowcount
        return poly_count + line_count + point_count


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
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
            extra_conditions = (" AND " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                SELECT
                    f.id, f.type_slug, f.role, f.name,
                    ST_AsText(f.geometry) AS geometry_wkt,
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
                    ST_AsText(f.geometry) AS geometry_wkt,
                    f.attributes,
                    f.created_at
                FROM serving_geo.feature_v f
                {where_clause}
                ORDER BY f.created_at, f.id
                LIMIT %s
            """
            params = params + [fetch_limit]

        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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


class PostgresExploreReader:
    def __init__(self, connection):
        self.connection = connection

    def _read_query(self, sql: str) -> list[dict]:
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
                    (aoi_id, scenario_id, indicator_id, value, aggregation_note, status),
                )
            return str(cur.fetchone()[0])

    def get_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        scenario_id: str | None = None,
    ) -> dict | None:
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if scenario_id is None:
                cur.execute(
                    """
                    SELECT id, aoi_id, scenario_id, indicator_id, value,
                           aggregation_note, status, computed_at
                    FROM curated_geo.aoi_indicator_summary
                    WHERE aoi_id = %s AND scenario_id IS NULL AND indicator_id = %s
                    """,
                    (aoi_id, indicator_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id, aoi_id, scenario_id, indicator_id, value,
                           aggregation_note, status, computed_at
                    FROM curated_geo.aoi_indicator_summary
                    WHERE aoi_id = %s AND scenario_id = %s AND indicator_id = %s
                    """,
                    (aoi_id, scenario_id, indicator_id),
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
        if scenario_id is None:
            conditions.append("scenario_id IS NULL")
        else:
            conditions.append("scenario_id = %s")
            params.append(scenario_id)
        if only_current:
            conditions.append("status = 'current'")

        where_clause = " AND ".join(conditions)
        with self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
