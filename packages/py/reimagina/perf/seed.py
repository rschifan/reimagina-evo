"""Deterministic data preparation for PostGIS performance benchmarks."""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from shapely.geometry import Polygon, box

from reimagina.data import aoi, catalog as cat
from reimagina.storage import backend_for_connection

from .manifest import DATASET_TIERS


PERF_SOURCE_PROVIDER = "reimagina-perf"
PERF_SOURCE_VERSION = "v1"
PERF_OBSERVATION_SOURCE_NAME = "Performance Observation Seed"
PERF_PROJECT_NAME = "Performance Benchmark Project"
PERF_AOI_NAMES = {
    "bbox_small": "perf_bbox_small",
    "bbox_dense": "perf_bbox_dense",
    "polygon_small": "perf_polygon_small",
    "polygon_dense": "perf_polygon_dense",
}


AOI_EXTENT_SQL = """
SELECT ST_XMin(geometry), ST_YMin(geometry), ST_XMax(geometry), ST_YMax(geometry)
FROM curated_geo.area_of_interest
WHERE id = %s
"""


def _require_postgres(conn) -> None:
    backend = backend_for_connection(conn)
    if backend.name != "postgres":
        raise ValueError("Performance seeding is Postgres/PostGIS-only.")


def _feature_extent(
    conn, aoi_id: str | None = None
) -> tuple[float, float, float, float]:
    """Bounds the benchmark boxes are cut from: every feature's extent, or
    the AOI ``aoi_id``'s. With two cities loaded the centre of the global
    extent lies between them and the boxes read nothing; an AOI keeps them
    inside one city."""
    with conn.cursor() as cur:
        if aoi_id is not None:
            cur.execute(AOI_EXTENT_SQL, (aoi_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"No AOI with id {aoi_id!r}.")
            return tuple(float(value) for value in row)
        cur.execute(
            """
            SELECT
              ST_XMin(extent::geometry),
              ST_YMin(extent::geometry),
              ST_XMax(extent::geometry),
              ST_YMax(extent::geometry)
            FROM (
              SELECT ST_Extent(geometry) AS extent
              FROM curated_geo.feature
            ) bounds
            """
        )
        row = cur.fetchone()
    if row is None or any(value is None for value in row):
        raise ValueError(
            "No canonical features are loaded. Seeding and benchmarks need feature data."
        )
    return tuple(float(value) for value in row)


def _bbox_from_extent(
    extent: tuple[float, float, float, float],
    *,
    width_ratio: float,
    height_ratio: float,
) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = extent
    width = max_x - min_x
    height = max_y - min_y
    center_x = min_x + width / 2
    center_y = min_y + height / 2
    half_width = max(width * width_ratio / 2, 0.0001)
    half_height = max(height * height_ratio / 2, 0.0001)
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def _bbox_wkt(bounds: tuple[float, float, float, float]) -> str:
    return box(*bounds).wkt


def _notched_polygon_wkt(bounds: tuple[float, float, float, float]) -> str:
    min_x, min_y, max_x, max_y = bounds
    mid_x = min_x + (max_x - min_x) * 0.55
    inner_x = min_x + (max_x - min_x) * 0.35
    mid_y = min_y + (max_y - min_y) * 0.5
    top_y = min_y + (max_y - min_y) * 0.8
    return Polygon(
        [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (mid_x, max_y),
            (mid_x, top_y),
            (inner_x, mid_y),
            (mid_x, min_y),
            (min_x, min_y),
        ]
    ).wkt


def _ensure_perf_project(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app_core.project WHERE name = %s", (PERF_PROJECT_NAME,)
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            """
            INSERT INTO app_core.project (name, description)
            VALUES (%s, %s)
            RETURNING id
            """,
            (PERF_PROJECT_NAME, "Deterministic performance benchmark workspace"),
        )
        return str(cur.fetchone()[0])


def _upsert_aoi(conn, *, project_id: str, name: str, geometry_wkt: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM curated_geo.area_of_interest
            WHERE project_id = %s AND name = %s
            """,
            (project_id, name),
        )
        row = cur.fetchone()
        if row:
            aoi_id = str(row[0])
            cur.execute(
                """
                UPDATE curated_geo.area_of_interest
                   SET geometry = ST_SetSRID(ST_GeomFromText(%s), 4326),
                       updated_at = now()
                 WHERE id = %s
                """,
                (geometry_wkt, aoi_id),
            )
            return aoi_id
    return aoi.create_aoi(
        conn,
        project_id=project_id,
        name=name,
        geometry_wkt=geometry_wkt,
        description="Deterministic AOI for performance benchmarks",
    )


def ensure_perf_aois(conn, aoi_id: str | None = None) -> dict[str, str]:
    _require_postgres(conn)
    extent = _feature_extent(conn, aoi_id)
    project_id = _ensure_perf_project(conn)
    geometries = {
        "bbox_small": _bbox_wkt(
            _bbox_from_extent(extent, width_ratio=0.02, height_ratio=0.02)
        ),
        "bbox_dense": _bbox_wkt(
            _bbox_from_extent(extent, width_ratio=0.08, height_ratio=0.08)
        ),
        "polygon_small": _bbox_wkt(
            _bbox_from_extent(extent, width_ratio=0.03, height_ratio=0.03)
        ),
        "polygon_dense": _notched_polygon_wkt(
            _bbox_from_extent(extent, width_ratio=0.1, height_ratio=0.08)
        ),
    }

    aoi_ids: dict[str, str] = {}
    for key, name in PERF_AOI_NAMES.items():
        aoi_id = _upsert_aoi(
            conn, project_id=project_id, name=name, geometry_wkt=geometries[key]
        )
        aoi.compute_aoi_features(conn, aoi_id)
        aoi_ids[key] = aoi_id
    return aoi_ids


def seed_observations(
    conn,
    *,
    dataset_tier: str,
    feature_limit: int | None = None,
) -> dict[str, object]:
    _require_postgres(conn)
    if dataset_tier not in DATASET_TIERS:
        raise ValueError(f"Unknown dataset tier: {dataset_tier!r}")

    tier = DATASET_TIERS[dataset_tier]
    observation_limit = feature_limit or tier.observation_feature_limit

    source_id = cat.register_source(
        conn,
        name=PERF_OBSERVATION_SOURCE_NAME,
        provider=PERF_SOURCE_PROVIDER,
        storage_type="tabular",
        integration_role="enrichment",
        storage_uri=f"generated://perf/observations/{dataset_tier}",
        version=PERF_SOURCE_VERSION,
        notes=f"Deterministic observation seed for {dataset_tier} performance benchmarks",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="perf",
        orchestrator_run_id=f"seed-{dataset_tier}-{observation_limit}-{uuid4().hex[:8]}",
        transformation_step="perf_seed_observations",
        code_version="perf-v1",
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH target_features AS (
              SELECT id, row_number() OVER (ORDER BY id) AS rn
              FROM curated_geo.feature
              ORDER BY id
              LIMIT %s
            ),
            generated AS (
              SELECT
                tf.id AS feature_id,
                attrs.attribute_key,
                attrs.value_type,
                CASE attrs.attribute_key
                  WHEN 'population_total' THEN (100 + (tf.rn %% 500))::double precision
                  WHEN 'ndvi_mean' THEN ((tf.rn %% 100)::double precision / 100.0)
                  WHEN 'footfall_index' THEN (10 + (tf.rn %% 1000))::double precision
                  ELSE NULL
                END AS value_numeric,
                CASE attrs.attribute_key
                  WHEN 'land_use_class' THEN
                    CASE tf.rn %% 4
                      WHEN 0 THEN 'residential'
                      WHEN 1 THEN 'mixed_use'
                      WHEN 2 THEN 'commercial'
                      ELSE 'institutional'
                    END
                  ELSE NULL
                END AS value_text,
                CASE attrs.attribute_key
                  WHEN 'access_profile' THEN jsonb_build_object(
                    'walk_score', (tf.rn %% 100),
                    'transit_score', ((tf.rn * 3) %% 100),
                    'green_score', ((tf.rn * 7) %% 100)
                  )
                  ELSE NULL
                END AS value_json
              FROM target_features tf
              CROSS JOIN (
                VALUES
                  ('population_total', 'numeric'),
                  ('ndvi_mean', 'numeric'),
                  ('footfall_index', 'numeric'),
                  ('land_use_class', 'text'),
                  ('access_profile', 'json')
              ) AS attrs(attribute_key, value_type)
            )
            INSERT INTO curated_geo.observation
              (feature_id, attribute_key, value_type,
               value_numeric, value_text, value_json,
               source_id, ingestion_run_id, lineage_ref, is_current, valid_from)
            SELECT
              g.feature_id,
              g.attribute_key,
              g.value_type,
              g.value_numeric,
              g.value_text,
              g.value_json,
              %s,
              %s,
              jsonb_build_object('dataset_tier', %s, 'seed_rn', row_number() OVER (ORDER BY g.feature_id, g.attribute_key)),
              true,
              now()
            FROM generated g
            WHERE NOT EXISTS (
              SELECT 1
              FROM curated_geo.observation o
              WHERE o.feature_id = g.feature_id
                AND o.attribute_key = g.attribute_key
                AND o.is_current
            )
            """,
            (observation_limit, source_id, run_id, dataset_tier),
        )
        inserted = max(cur.rowcount, 0)
        cur.execute("ANALYZE curated_geo.observation")
        cur.execute("ANALYZE curated_geo.feature")
        cur.execute("ANALYZE curated_geo.aoi_feature")
        cur.execute("ANALYZE curated_geo.area_of_interest")

    cat.complete_ingestion_run(
        conn,
        run_id,
        status="succeeded",
        details={
            "dataset_tier": dataset_tier,
            "feature_limit": observation_limit,
            "records_written": inserted,
        },
    )

    return {
        "source_id": source_id,
        "ingestion_run_id": run_id,
        "dataset_tier": dataset_tier,
        "feature_limit": observation_limit,
        "records_written": inserted,
    }


def prepare_benchmark_dataset(
    conn,
    *,
    dataset_tier: str,
    feature_limit: int | None = None,
    aoi_id: str | None = None,
) -> dict[str, object]:
    if dataset_tier not in DATASET_TIERS:
        raise ValueError(f"Unknown dataset tier: {dataset_tier!r}")

    observation_seed = seed_observations(
        conn,
        dataset_tier=dataset_tier,
        feature_limit=feature_limit,
    )
    aoi_ids = ensure_perf_aois(conn, aoi_id)
    return {
        "dataset_tier": dataset_tier,
        "tier": asdict(DATASET_TIERS[dataset_tier]),
        "observation_seed": observation_seed,
        "aoi_ids": aoi_ids,
    }


# Every row the seed writes is found by a mark it already carries, so rows
# seeded before this command existed are found the same way: the perf source
# by its provider, its observations and runs through that source, the AOIs by
# name inside the perf project. A same-named AOI in another project is not one.
# Child tables are deleted explicitly, before their cascades would, so each
# count is reported.
_PERF_SOURCES = (
    "SELECT id FROM curated_geo.data_source_catalog WHERE provider = %(provider)s"
)
_PERF_PROJECTS = "SELECT id FROM app_core.project WHERE name = %(project)s"
_PERF_AOIS = (
    "SELECT id FROM curated_geo.area_of_interest "
    f"WHERE project_id IN ({_PERF_PROJECTS}) AND name = ANY(%(aoi_names)s)"
)
_CLEANUP_STEPS = (
    ("curated_geo.aoi_indicator_summary", f"aoi_id IN ({_PERF_AOIS})"),
    ("curated_geo.aoi_feature", f"aoi_id IN ({_PERF_AOIS})"),
    ("curated_geo.area_of_interest", f"id IN ({_PERF_AOIS})"),
    # Only once empty: a scenario someone made in it is not perf data, and
    # deleting the project would cascade it away.
    (
        "app_core.project",
        f"id IN ({_PERF_PROJECTS}) AND NOT EXISTS (SELECT 1 FROM "
        "curated_geo.area_of_interest a WHERE a.project_id = app_core.project.id) "
        "AND NOT EXISTS (SELECT 1 FROM app_core.scenario s "
        "WHERE s.project_id = app_core.project.id)",
    ),
    ("curated_geo.observation", f"source_id IN ({_PERF_SOURCES})"),
    ("curated_geo.ingestion_run", f"source_id IN ({_PERF_SOURCES})"),
    ("curated_geo.data_source_catalog", f"id IN ({_PERF_SOURCES})"),
)
# Deleting a perf AOI would null or orphan these columns on rows that are not
# perf data, so their presence stops the cleanup instead. Compared as text:
# `orchestration_run.aoi_id` is a text column.
_AOI_REFERENCES = (
    ("app_core.scenario", "aoi_id"),
    ("curated_geo.aggregation_recipe", "aoi_id"),
    ("jobs_runtime.orchestration_run", "aoi_id"),
)


def cleanup_perf_data(conn, *, dry_run: bool = False) -> dict[str, object]:
    """Delete every row `seed_observations`/`ensure_perf_aois` wrote, in the
    caller's transaction. ``dry_run`` runs the same deletes and rolls them
    back, so the counts it reports are exactly what a real run removes. It
    therefore takes the same row locks for its duration: on a shared database,
    writers touching the perf rows wait until it finishes. A count-only preview
    would be lock-free but could disagree with the real run, whose later steps
    (the project) depend on the earlier deletes."""
    _require_postgres(conn)
    params = {
        "provider": PERF_SOURCE_PROVIDER,
        "project": PERF_PROJECT_NAME,
        "aoi_names": list(PERF_AOI_NAMES.values()),
    }
    removed: dict[str, int] = {}
    with conn.cursor() as cur:
        for table, column in _AOI_REFERENCES:
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE {column}::text IN "
                f"(SELECT id::text FROM ({_PERF_AOIS}) perf_aoi)",
                params,
            )
            if cur.fetchone()[0]:
                raise ValueError(
                    f"{table} rows reference a perf AOI; repoint or delete them "
                    "before running perf cleanup."
                )
        cur.execute("SAVEPOINT perf_cleanup")
        for table, where in _CLEANUP_STEPS:
            cur.execute(f"DELETE FROM {table} WHERE {where}", params)
            removed[table] = cur.rowcount
        if dry_run:
            cur.execute("ROLLBACK TO SAVEPOINT perf_cleanup")
        elif removed["curated_geo.observation"]:
            # The seed refreshed it to publish the rows; refresh it to unpublish.
            cur.execute(
                "REFRESH MATERIALIZED VIEW serving_geo.feature_current_attributes_mv"
            )
        cur.execute("RELEASE SAVEPOINT perf_cleanup")
    return {"dry_run": dry_run, "removed": removed}
