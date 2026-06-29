"""Postgres<->DuckDB parity suite for the storage repositories.

This is the safety net for the upcoming base-class refactor of the storage
repositories, and the standing mitigation for the "DuckDB-Python validator vs
Postgres trigger" mirror. It does NOT re-create the full DuckDB contract suite
(``tests/integration/test_duckdb_storage_contracts.py``); it pins only the two
highest-divergence-risk surfaces:

  1. Geodesic numeric indicators (``building_density`` + ``compactness``), which
     are computed by *different* SQL on each engine -- Postgres measures via the
     ``::geography`` cast, DuckDB reprojects to UTM 14N and measures planar
     metres. They MUST still agree within a documented tolerance.
  2. The ``indicator_result`` write validator, enforced by the Postgres
     ``indicator_result_before_write`` trigger on one side and the Python
     ``_validate_indicator_result_write`` mirror on the other. Both must reject
     the same writes.

The same backend-agnostic seed+compute flow (``_seed_and_compute``) runs on both
engines -- only the connection differs -- so a refactor that quietly changes a
value or relaxes a validation rule on one backend turns this suite red.

Skip behaviour: the Postgres-backed ``conn`` fixture (root ``conftest.py``)
``pytest.skip``s when PG is unreachable. Parity tests that need both backends
therefore skip cleanly on a PG-less CI box; the DuckDB half always runs.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import psycopg2
import pytest
from shapely.geometry import Polygon

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.db import bootstrap_local_platform, connect
from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.storage import backend_for_connection

# --- Documented parity tolerance ------------------------------------------------
#
# Postgres computes geodesic area/perimeter on the WGS84 spheroid via the
# ``::geography`` cast; DuckDB projects to UTM 14N (EPSG:32614, CDMX's local
# zone) and measures in the *planar* projected plane. These are two different
# measurement models, so the two engines do NOT produce byte-identical metres --
# UTM has scale distortion that grows with distance from the zone's central
# meridian, and the spheroidal vs. planar treatment of area/length differs.
#
# For the small CDMX-latitude (~19.43 deg N, near the UTM-14N meridian)
# polygons we seed, that systematic difference is well under 0.1%. We assert a
# relative tolerance of 1e-3 (0.1%) -- loose enough to absorb the legitimate
# geography-vs-UTM gap, tight enough that any refactor changing a value beyond
# that (e.g. a wrong CRS, a unit slip, an aggregation bug) is caught.
#
# Measured divergence on this seed (PG geography vs DuckDB UTM-14N planar):
#   * compactness  : ~1e-9  -- effectively identical. Polsby-Popper is
#     4*pi*area / perimeter^2; the UTM scale factor k appears as k^2/k^2 and
#     cancels, so projection distortion drops out of the ratio entirely.
#   * building_density : ~8e-4 (0.08%) -- this is the real, irreducible
#     geography-vs-UTM AREA gap surfacing directly, because density is
#     count / hectares (one raw area measurement, nothing to cancel against).
# The worst real divergence (~7.96e-4) sits just under the 1e-3 bound, so the
# tolerance has margin but is not slack: a refactor that shifts a value by more
# than ~0.1% breaks the test instead of passing silently.
PARITY_REL_TOL = 1e-3
# An absolute floor so values legitimately at/near 0 (e.g. a perfectly thin
# rectangle's compactness) don't fail relative comparison against ~0.
PARITY_ABS_TOL = 1e-6


def _duckdb_dsn(path: Path) -> str:
    return f"duckdb:////{path}"


# --- Fixed geometries, shared by both backends ---------------------------------
#
# Three blocks of known, distinct shape near Mexico City (lon ~-99.13, lat
# ~19.43). A ~0.004 deg square is ~440 m on a side, so the geodesic metres are
# order-of-magnitude sane and the three compactness values are well separated.
_SQUARE_BLOCK = Polygon(
    [
        (-99.140, 19.430),
        (-99.136, 19.430),
        (-99.136, 19.434),
        (-99.140, 19.434),
    ]
)
_THIN_RECT_BLOCK = Polygon(
    [
        (-99.130, 19.430),
        (-99.126, 19.430),
        (-99.126, 19.4301),
        (-99.130, 19.4301),
    ]
)


def _near_circle_block() -> Polygon:
    import math

    cx, cy, r = -99.120, 19.432, 0.002
    return Polygon(
        [
            (cx + r * math.cos(2 * math.pi * i / 32), cy + r * math.sin(2 * math.pi * i / 32))
            for i in range(32)
        ]
    )


def _tiny_building(lon: float, lat: float) -> Polygon:
    """A ~11 m footprint whose centroid lands cleanly inside one block."""
    return Polygon(
        [
            (lon, lat),
            (lon + 0.0001, lat),
            (lon + 0.0001, lat + 0.0001),
            (lon, lat + 0.0001),
        ]
    )


_BLOCKS = gpd.GeoDataFrame(
    [
        {"id": "sq", "geometry": _SQUARE_BLOCK},
        {"id": "rect", "geometry": _THIN_RECT_BLOCK},
        {"id": "circle", "geometry": _near_circle_block()},
    ],
    crs="EPSG:4326",
)

# Buildings: three with centroids inside the square block, one inside the
# near-circle block, zero in the thin rectangle. This gives every density
# outcome (dense / sparse / measured-zero) a presence on both engines.
_BUILDINGS = gpd.GeoDataFrame(
    [
        {"id": "bd-sq-1", "geometry": _tiny_building(-99.139, 19.431)},
        {"id": "bd-sq-2", "geometry": _tiny_building(-99.138, 19.432)},
        {"id": "bd-sq-3", "geometry": _tiny_building(-99.1375, 19.4315)},
        {"id": "bd-circle-1", "geometry": _tiny_building(-99.1205, 19.4315)},
    ],
    crs="EPSG:4326",
)


def _seed_and_compute(conn) -> dict[str, dict[str, dict[str, float]]]:
    """Backend-agnostic seed + compute. Runs verbatim on Postgres and DuckDB.

    Seeds the IDENTICAL blocks + buildings, runs both geodesic indicators, and
    reads the per-feature results back through the serving contract. Returns
    ``{indicator_slug: {source_record_ref: {"value": float, "quality": str}}}``
    keyed by the stable ``source_record_ref`` (not the per-engine UUID) so the
    two backends can be compared directly.
    """
    # Idempotent on both engines (ON CONFLICT DO NOTHING); a no-op on Postgres
    # where migrations already seeded the feature-type catalog.
    feat.seed_feature_types(conn)
    seed_catalog(conn)

    source_id = cat.register_source(
        conn,
        name=f"parity_src_{uuid4().hex}",
        provider="pytest",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file:///tmp/parity.geojson",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="silver_to_curated",
        code_version="test",
    )

    block_type_id = feat.get_type_id(conn, "MEX", "block")
    building_type_id = feat.get_type_id(conn, "global", "building")

    feat.load_features(
        conn,
        _BLOCKS,
        source_id=source_id,
        ingestion_run_id=run_id,
        type_id=block_type_id,
        dataset_version="v1",
        source_record_ref_col="id",
    )
    feat.load_features(
        conn,
        _BUILDINGS,
        source_id=source_id,
        ingestion_run_id=run_id,
        type_id=building_type_id,
        dataset_version="v1",
        source_record_ref_col="id",
    )

    compute_block_compactness(conn, code_version="test")
    compute_block_building_density(conn, code_version="test")

    result_repo = backend_for_connection(conn).indicator_result_repo()
    out: dict[str, dict[str, dict[str, float]]] = {}
    for slug in (
        "urban_form_block_compactness",
        "urban_form_block_building_density",
    ):
        per_ref: dict[str, dict[str, float]] = {}
        for row in result_repo.list_current_for_indicator(indicator_slug=slug):
            ref = _source_ref_for_feature(conn, row["feature_id"])
            per_ref[ref] = {
                "value": float(row["value_numeric"]),
                "quality": row["quality_flag"],
            }
        out[slug] = per_ref
    return out


def _is_postgres(conn) -> bool:
    """True for a psycopg2 connection, False for DuckDB.

    Both connection objects expose ``.cursor()``/``.execute()``, so we branch on
    the psycopg2-specific module rather than duck-typing a method.
    """
    return isinstance(conn, psycopg2.extensions.connection)


def _source_ref_for_feature(conn, feature_id: str) -> str:
    """Resolve a feature UUID back to its stable source_record_ref on either engine."""
    if _is_postgres(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_record_ref FROM curated_geo.feature WHERE id = %s",
                (feature_id,),
            )
            return cur.fetchone()[0]
    return conn.execute(
        "SELECT source_record_ref FROM curated_geo.feature WHERE CAST(id AS VARCHAR) = ?",
        [feature_id],
    ).fetchone()[0]


@pytest.fixture
def duckdb_conn(tmp_path):
    """A freshly bootstrapped DuckDB connection (always available)."""
    app_dsn = _duckdb_dsn(tmp_path / "parity_app.duckdb")
    test_dsn = _duckdb_dsn(tmp_path / "parity_test.duckdb")
    bootstrap_local_platform(app_dsn=app_dsn, test_dsn=test_dsn)
    with connect(app_dsn) as conn:
        yield conn


# ------------------------------------------------------------------------------
# 1. Numeric indicator parity
# ------------------------------------------------------------------------------
def test_geodesic_indicator_parity(conn, duckdb_conn):
    """building_density + compactness agree across engines within tolerance.

    The IDENTICAL features are seeded into Postgres (``conn`` fixture, geodesic
    via ``::geography``) and DuckDB (``duckdb_conn``, UTM-14N planar metres).
    Per-feature indicator values must match within ``PARITY_REL_TOL`` -- the
    documented allowance for the geography-vs-UTM measurement-model difference.

    Skips (does not fail) when Postgres is unreachable: the ``conn`` fixture
    skips via ``migrated_db``, so this whole test is skipped on a PG-less box.
    """
    pg = _seed_and_compute(conn)
    duck = _seed_and_compute(duckdb_conn)

    for slug in (
        "urban_form_block_compactness",
        "urban_form_block_building_density",
    ):
        pg_vals = pg[slug]
        duck_vals = duck[slug]
        assert pg_vals.keys() == duck_vals.keys(), (
            f"{slug}: backends produced rows for different features: "
            f"PG={sorted(pg_vals)} DuckDB={sorted(duck_vals)}"
        )
        for ref, pg_row in pg_vals.items():
            duck_row = duck_vals[ref]
            # Quality flags are engine-independent metadata -- they must be
            # byte-identical, no tolerance.
            assert pg_row["quality"] == duck_row["quality"], (
                f"{slug}/{ref}: quality_flag diverged "
                f"PG={pg_row['quality']!r} DuckDB={duck_row['quality']!r}"
            )
            assert pg_row["value"] == pytest.approx(
                duck_row["value"], rel=PARITY_REL_TOL, abs=PARITY_ABS_TOL
            ), (
                f"{slug}/{ref}: value diverged beyond the geography-vs-UTM "
                f"tolerance (rel={PARITY_REL_TOL}, abs={PARITY_ABS_TOL}): "
                f"PG={pg_row['value']!r} DuckDB={duck_row['value']!r}"
            )

    # Sanity: the seed actually exercised all three density outcomes on the
    # geodesic-measuring Postgres backend, so the parity assertions above
    # weren't vacuous. (We assert presence/zero rather than a count ratio: the
    # blocks have different areas, so density = count/hectares is not a clean
    # 3:1 between the 3-building square and the 1-building circle.)
    density = pg["urban_form_block_building_density"]
    assert density["sq"]["value"] > 0.0  # 3 buildings inside
    assert density["circle"]["value"] > 0.0  # 1 building inside
    assert density["rect"]["value"] == 0.0  # empty -> measured zero, not missing
    assert density["rect"]["quality"] == "measured"
    # Compactness ordering is also engine-independent signal worth pinning:
    # thin rectangle ~0, square ~0.78, near-circle ~1.
    compact = pg["urban_form_block_compactness"]
    assert compact["rect"]["value"] < compact["sq"]["value"] < compact["circle"]["value"]


# ------------------------------------------------------------------------------
# 2. Validation parity
# ------------------------------------------------------------------------------
def _seed_one_block_for_validation(conn) -> tuple[object, str]:
    """Seed catalog + a single block, return (job_run_repo job id, feature_id).

    Shared setup for the validation-parity tests. Runs identically on both
    engines.
    """
    feat.seed_feature_types(conn)
    seed_catalog(conn)
    source_id = cat.register_source(
        conn,
        name=f"parity_val_src_{uuid4().hex}",
        provider="pytest",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file:///tmp/parity_val.geojson",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="silver_to_curated",
        code_version="test",
    )
    block_type_id = feat.get_type_id(conn, "MEX", "block")
    feat.load_features(
        conn,
        gpd.GeoDataFrame([{"id": "blk-val", "geometry": _SQUARE_BLOCK}], crs="EPSG:4326"),
        source_id=source_id,
        ingestion_run_id=run_id,
        type_id=block_type_id,
        dataset_version="v1",
        source_record_ref_col="id",
    )
    feature_id = _feature_id_for_ref(conn, "blk-val")
    job_repo = backend_for_connection(conn).job_run_repo()
    job_id = job_repo.start_job(
        model_family="urban_form", code_version="test", status="running"
    )
    return job_id, feature_id


def _feature_id_for_ref(conn, ref: str) -> str:
    if _is_postgres(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM curated_geo.feature WHERE source_record_ref = %s",
                (ref,),
            )
            return cur.fetchone()[0]
    return conn.execute(
        "SELECT CAST(id AS VARCHAR) FROM curated_geo.feature WHERE source_record_ref = ?",
        [ref],
    ).fetchone()[0]


# Postgres enforces the rule via the ``indicator_result_before_write`` trigger,
# which surfaces in psycopg2 as a ``psycopg2.Error`` *after* the INSERT (the
# transaction is then aborted -- hence the rollback in each PG branch). DuckDB
# enforces the same rule in Python *before* any SQL, raising ``ValueError``. The
# expected exception type therefore legitimately differs per engine; what must
# be IDENTICAL is that both refuse the write.
_PG_REJECT = psycopg2.Error
_DUCK_REJECT = ValueError


def test_validation_parity_value_out_of_range(conn, duckdb_conn):
    """A value above the catalog ``valid_max`` is rejected by BOTH backends.

    ``urban_form_block_compactness`` has valid_max=1.0; writing 2.0 must be
    refused -- by the Postgres trigger and by the DuckDB Python mirror. Pins
    that the two validators stay equivalent on bound enforcement.

    Skips when Postgres is unreachable (``conn`` fixture skips via
    ``migrated_db``); the DuckDB assertion alone would otherwise still need PG to
    have run, so we keep both in one test to assert true parity.
    """
    # --- DuckDB side (always runs) ---
    duck_job, duck_feature = _seed_one_block_for_validation(duckdb_conn)
    duck_repo = backend_for_connection(duckdb_conn).indicator_result_repo()
    with pytest.raises(_DUCK_REJECT, match="above valid_max"):
        duck_repo.write_result(
            feature_id=duck_feature,
            indicator_slug="urban_form_block_compactness",
            job_run_id=duck_job,
            value_numeric=2.0,
        )

    # --- Postgres side ---
    pg_job, pg_feature = _seed_one_block_for_validation(conn)
    pg_repo = backend_for_connection(conn).indicator_result_repo()
    with pytest.raises(_PG_REJECT):
        pg_repo.write_result(
            feature_id=pg_feature,
            indicator_slug="urban_form_block_compactness",
            job_run_id=pg_job,
            value_numeric=2.0,
        )
    # The trigger raised mid-transaction; the PG connection is now in an aborted
    # state and must be rolled back before teardown can use it.
    conn.rollback()


def test_validation_parity_exactly_one_value_column(conn, duckdb_conn):
    """The "exactly one value column" rule is enforced by BOTH backends.

    quality_flag='measured' with NO value column set must be refused -- the
    trigger / mirror both require exactly one of value_numeric/value_text/
    value_json. Pins the value-column-cardinality rule across engines.
    """
    # --- DuckDB side (always runs) ---
    duck_job, duck_feature = _seed_one_block_for_validation(duckdb_conn)
    duck_repo = backend_for_connection(duckdb_conn).indicator_result_repo()
    with pytest.raises(_DUCK_REJECT, match="exactly one value column"):
        duck_repo.write_result(
            feature_id=duck_feature,
            indicator_slug="urban_form_block_compactness",
            job_run_id=duck_job,
            quality_flag="measured",
            # deliberately no value_numeric / value_text / value_json
        )

    # --- Postgres side ---
    pg_job, pg_feature = _seed_one_block_for_validation(conn)
    pg_repo = backend_for_connection(conn).indicator_result_repo()
    with pytest.raises(_PG_REJECT):
        pg_repo.write_result(
            feature_id=pg_feature,
            indicator_slug="urban_form_block_compactness",
            job_run_id=pg_job,
            quality_flag="measured",
        )
    conn.rollback()
