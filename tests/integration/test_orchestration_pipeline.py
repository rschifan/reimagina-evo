"""End-to-end: impact engine → coordinator → real indicator_result rows.

Proves the Phase 3 domain core wires together against Postgres: a geometry
edit produces a dirty-set, the coordinator runs the shipped indicators'
real compute fns through the runner registry, and current values land.
"""

from __future__ import annotations

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    seed_catalog,
)
from reimagina.orchestration.coordinator import run_dirty_set
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.runners import default_runners
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)


def _seed_block(conn) -> str:
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="orch_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    return _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="orch-blk-1",
        geometry_wkt=(
            "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
            "-99.140 19.433,-99.140 19.430))"
        ),
    )


def test_geometry_edit_recomputes_shipped_indicators(conn):
    block_id = _seed_block(conn)

    # Both shipped indicators read geometry, so a geometry edit dirties both.
    dirty = compute_dirty_set({"geometry"}, {block_id}, SHIPPED_INDICATORS)
    assert {u.indicator for u in dirty} == {
        URBAN_FORM_BLOCK_COMPACTNESS.slug,
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
    }

    report = run_dirty_set(
        conn,
        dirty,
        SHIPPED_INDICATORS,
        default_runners(),
        aoi_id=None,
        code_version="test",
    )
    assert report.status == "succeeded", report.by_indicator()

    # Compactness landed a bounded current value for the seeded block.
    result = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .get_current(
            feature_id=block_id,
            indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug,
        )
    )
    assert result is not None
    assert 0.0 <= result["value_numeric"] <= 1.0
