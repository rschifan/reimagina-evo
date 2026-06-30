"""End-to-end: intake → impact engine → coordinator → real indicator_result rows.

Proves the Phase 3 domain core wires together against Postgres through its
public entry point: a geometry edit, fed to ``evaluate_scenario``, produces a
dirty-set, runs the shipped indicators' real compute fns through the runner
registry, and lands current values.
"""

from __future__ import annotations

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    seed_catalog,
)
from reimagina.orchestration.intake import evaluate_scenario
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)


def _seed_two_blocks(conn) -> tuple[str, str]:
    """Seed catalog + two disjoint MEX:block features; return their ids."""
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="orch_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    def _block(ref: str, wkt: str) -> str:
        return _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=wkt,
        )

    block_a = _block(
        "orch-blk-a",
        "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
        "-99.140 19.433,-99.140 19.430))",
    )
    block_b = _block(
        "orch-blk-b",
        "POLYGON((-99.130 19.430,-99.127 19.430,-99.127 19.433,"
        "-99.130 19.433,-99.130 19.430))",
    )
    return block_a, block_b


def test_geometry_edit_recomputes_only_the_dirty_feature(conn):
    block_a, block_b = _seed_two_blocks(conn)

    # Edit block_a only; both shipped indicators read geometry → both dirty.
    report = evaluate_scenario(
        conn,
        changed_attributes={"geometry"},
        edited_features={block_a},
        code_version="test",
    )
    assert report.status == "succeeded", report.by_indicator()
    assert {o.indicator for o in report.outcomes} == {
        URBAN_FORM_BLOCK_COMPACTNESS.slug,
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
    }

    repo = backend_for_connection(conn).indicator_result_repo()
    edited = repo.get_current(
        feature_id=block_a, indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug
    )
    untouched = repo.get_current(
        feature_id=block_b, indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug
    )
    # Selective recompute: only the edited block got a value.
    assert edited is not None
    assert 0.0 <= edited["value_numeric"] <= 1.0
    assert untouched is None
