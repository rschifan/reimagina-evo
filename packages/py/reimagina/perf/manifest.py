"""Benchmark manifest for production-facing PostGIS performance checks."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyBudget:
    p50_ms: float
    p95_ms: float
    p99_ms: float


@dataclass(frozen=True)
class DatasetTier:
    name: str
    description: str
    observation_feature_limit: int
    selected_feature_count: int
    iterations: int


@dataclass(frozen=True)
class BenchmarkSpec:
    name: str
    description: str
    sql: str
    parameter_builder: str
    expected_node_any_of: tuple[str, ...]
    expected_index_any_of: tuple[str, ...]
    hot_tables_no_seq_scan: tuple[str, ...]
    row_count_min: int
    latency_budgets: dict[str, LatencyBudget]
    statement_timeout_ms: int = 5000


DATASET_TIERS: dict[str, DatasetTier] = {
    "smoke": DatasetTier(
        name="smoke",
        description="Small deterministic benchmark seed for CI and local verification.",
        observation_feature_limit=250,
        selected_feature_count=25,
        iterations=3,
    ),
    "representative": DatasetTier(
        name="representative",
        description="Mexico City-scale benchmark profile for staging and release checks.",
        observation_feature_limit=25_000,
        selected_feature_count=250,
        iterations=5,
    ),
    "stress": DatasetTier(
        name="stress",
        description="Higher-volume observation and AOI profile for deeper capacity checks.",
        observation_feature_limit=100_000,
        selected_feature_count=500,
        iterations=7,
    ),
}


_BBOX_FEATURE_SQL = """
SELECT
  f.id,
  f.type_slug,
  f.role,
  ST_AsBinary(f.geometry) AS geometry
FROM serving_geo.feature_v f
WHERE f.geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
  AND ST_Intersects(f.geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
LIMIT 5000
"""

_POLYGON_FEATURE_SQL = """
SELECT
  f.id,
  f.type_slug,
  f.role,
  ST_AsBinary(f.geometry) AS geometry
FROM serving_geo.feature_v f
WHERE f.geometry && ST_Envelope(ST_GeomFromText(%s, 4326))
  AND ST_Intersects(f.geometry, ST_GeomFromText(%s, 4326))
LIMIT 5000
"""

_BBOX_FEATURE_ATTRIBUTES_SQL = """
SELECT
  f.id,
  f.type_slug,
  f.role,
  f.attributes
FROM serving_geo.feature_v f
WHERE f.geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
  AND ST_Intersects(f.geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
LIMIT 5000
"""

_SELECTED_CURRENT_OBSERVATIONS_SQL = """
SELECT
  o.feature_id,
  o.attribute_key,
  o.value_type,
  o.value_numeric,
  o.value_text,
  o.value_json
FROM curated_geo.observation o
WHERE o.is_current
  AND o.feature_id = ANY(%s::uuid[])
ORDER BY o.feature_id, o.attribute_key
"""

_FEATURE_DETAIL_SQL = """
SELECT
  f.id,
  f.type_slug,
  f.role,
  f.name,
  f.attributes
FROM serving_geo.feature_v f
WHERE f.id = %s
"""

_AOI_MEMBERSHIP_SQL = """
SELECT
  f.id,
  f.type_slug,
  f.role,
  f.overlap_ratio
FROM serving_geo.feature_aoi_v f
WHERE f.aoi_id = %s
LIMIT 5000
"""

# Tile benchmarks — call get_tile() directly to measure end-to-end MVT cost.
# z=14 is the highest detail level (full attributes + no simplification).
# z=12 is the most common interactive zoom (simplified + base attributes only).
# z=8  exercises the low-zoom code path (context-only GIST index + no join).
_TILE_Z14_SQL = "SELECT serving_geo.get_tile(%s, %s, %s, '{}'::json)"
_TILE_Z12_SQL = "SELECT serving_geo.get_tile(%s, %s, %s, '{}'::json)"
_TILE_Z8_SQL  = "SELECT serving_geo.get_tile(%s, %s, %s, '{}'::json)"
_TILE_FILTERED_SQL = "SELECT serving_geo.get_tile(%s, %s, %s, %s::json)"


BENCHMARKS: dict[str, BenchmarkSpec] = {
    "bbox_features_small": BenchmarkSpec(
        name="bbox_features_small",
        description="Small viewport-style read over serving_geo.feature_v.",
        sql=_BBOX_FEATURE_SQL,
        parameter_builder="bbox_small",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("feature_geometry_gist", "feature_block_geom_idx", "feature_lot_geom_idx"),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(25, 80, 150),
            "representative": LatencyBudget(120, 350, 600),
            "stress": LatencyBudget(180, 450, 800),
        },
    ),
    "bbox_features_dense": BenchmarkSpec(
        name="bbox_features_dense",
        description="Larger dense viewport read over serving_geo.feature_v.",
        sql=_BBOX_FEATURE_SQL,
        parameter_builder="bbox_dense",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("feature_geometry_gist",),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(40, 120, 220),
            "representative": LatencyBudget(250, 900, 1500),
            "stress": LatencyBudget(350, 1200, 2000),
        },
    ),
    "polygon_features_small": BenchmarkSpec(
        name="polygon_features_small",
        description="Small polygon read over serving_geo.feature_v.",
        sql=_POLYGON_FEATURE_SQL,
        parameter_builder="polygon_small",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("feature_geometry_gist",),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(35, 90, 180),
            "representative": LatencyBudget(180, 450, 800),
            "stress": LatencyBudget(250, 700, 1200),
        },
    ),
    "polygon_features_dense": BenchmarkSpec(
        name="polygon_features_dense",
        description="Complex polygon read over serving_geo.feature_v.",
        sql=_POLYGON_FEATURE_SQL,
        parameter_builder="polygon_dense",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("feature_geometry_gist",),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(50, 120, 240),
            "representative": LatencyBudget(300, 1200, 2000),
            "stress": LatencyBudget(450, 1600, 2600),
        },
    ),
    "bbox_features_with_current_attributes": BenchmarkSpec(
        name="bbox_features_with_current_attributes",
        description="Viewport read over serving_geo.feature_v with merged current attributes.",
        sql=_BBOX_FEATURE_ATTRIBUTES_SQL,
        parameter_builder="bbox_dense",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("feature_geometry_gist", "obs_one_current"),
        hot_tables_no_seq_scan=("feature", "observation"),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(50, 130, 250),
            "representative": LatencyBudget(350, 1200, 1800),
            "stress": LatencyBudget(500, 1600, 2400),
        },
    ),
    "selected_features_current_observations": BenchmarkSpec(
        name="selected_features_current_observations",
        description="Current observation lookup for a selected visible feature set.",
        sql=_SELECTED_CURRENT_OBSERVATIONS_SQL,
        parameter_builder="selected_feature_ids",
        expected_node_any_of=("Index Only Scan", "Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("obs_one_current", "obs_feature_key_idx"),
        hot_tables_no_seq_scan=("observation",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(25, 80, 150),
            "representative": LatencyBudget(120, 400, 700),
            "stress": LatencyBudget(180, 600, 1000),
        },
    ),
    "feature_detail_by_id": BenchmarkSpec(
        name="feature_detail_by_id",
        description="Single-feature detail read through serving_geo.feature_v.",
        sql=_FEATURE_DETAIL_SQL,
        parameter_builder="feature_id",
        expected_node_any_of=("Index Scan", "Index Only Scan"),
        expected_index_any_of=("feature_pkey",),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(10, 40, 80),
            "representative": LatencyBudget(20, 80, 150),
            "stress": LatencyBudget(30, 100, 180),
        },
    ),
    "aoi_membership_read": BenchmarkSpec(
        name="aoi_membership_read",
        description="Precomputed AOI membership read through serving_geo.feature_aoi_v.",
        sql=_AOI_MEMBERSHIP_SQL,
        parameter_builder="aoi_dense_id",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan"),
        expected_index_any_of=("aoi_feature_pkey", "aoi_feature_feature_idx"),
        hot_tables_no_seq_scan=("aoi_feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(20, 70, 140),
            "representative": LatencyBudget(120, 350, 700),
            "stress": LatencyBudget(180, 550, 900),
        },
    ),

    # ── Tile benchmarks ────────────────────────────────────────────────────────
    # These measure the full MVT pipeline: GIST scan → simplify → ST_AsMVTGeom
    # → ST_AsMVT.  The tile function is the primary serving path for the
    # frontend map; its latency is the most user-visible SLO in the system.
    "tile_z14_small": BenchmarkSpec(
        name="tile_z14_small",
        description=(
            "MVT tile at z=14 (full detail, no simplification, merged attributes). "
            "Represents a street-level zoom over a dense urban area."
        ),
        sql=_TILE_Z14_SQL,
        parameter_builder="tile_z14_xyz",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan", "Function Scan"),
        expected_index_any_of=(
            "feature_geometry_gist",
            "feature_block_geom_idx",
            "feature_lot_geom_idx",
            "feature_current_attributes_mv_feature_id_idx",
        ),
        hot_tables_no_seq_scan=("feature", "feature_current_attributes_mv"),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(20, 60, 120),
            "representative": LatencyBudget(80, 200, 400),
            "stress": LatencyBudget(120, 300, 600),
        },
    ),
    "tile_z12_dense": BenchmarkSpec(
        name="tile_z12_dense",
        description=(
            "MVT tile at z=12 (simplified geometry, base attributes only, no MV join). "
            "Represents neighbourhood zoom — most common interactive viewport."
        ),
        sql=_TILE_Z12_SQL,
        parameter_builder="tile_z12_xyz",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan", "Function Scan"),
        expected_index_any_of=(
            "feature_geometry_gist",
            "feature_current_attributes_mv_feature_id_idx",
        ),
        hot_tables_no_seq_scan=("feature", "feature_current_attributes_mv"),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(15, 50, 100),
            "representative": LatencyBudget(50, 150, 300),
            "stress": LatencyBudget(80, 220, 450),
        },
    ),
    "tile_z8_overview": BenchmarkSpec(
        name="tile_z8_overview",
        description=(
            "MVT tile at z=8 (heavy simplification, no attributes, context features only). "
            "Exercises the feature_context_geom_idx partial GIST index."
        ),
        sql=_TILE_Z8_SQL,
        parameter_builder="tile_z8_xyz",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan", "Function Scan"),
        expected_index_any_of=(
            "feature_context_geom_idx",
            "feature_geometry_gist",
        ),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=0,  # May be empty at overview zoom for small datasets
        latency_budgets={
            "smoke": LatencyBudget(5, 20, 40),
            "representative": LatencyBudget(15, 50, 100),
            "stress": LatencyBudget(25, 80, 160),
        },
    ),
    "tile_z14_filtered_type": BenchmarkSpec(
        name="tile_z14_filtered_type",
        description=(
            "MVT tile at z=14 filtered by type_slug=building. "
            "Validates that the partial GIST index (feature_block_geom_idx / "
            "feature_lot_geom_idx) is used for high-selectivity type filters."
        ),
        sql=_TILE_FILTERED_SQL,
        parameter_builder="tile_z14_xyz_building",
        expected_node_any_of=("Index Scan", "Bitmap Heap Scan", "Function Scan"),
        expected_index_any_of=(
            "feature_geometry_gist",
            "feature_type_slug_idx",
        ),
        hot_tables_no_seq_scan=("feature",),
        row_count_min=1,
        latency_budgets={
            "smoke": LatencyBudget(15, 50, 100),
            "representative": LatencyBudget(60, 180, 350),
            "stress": LatencyBudget(90, 250, 500),
        },
    ),
}
