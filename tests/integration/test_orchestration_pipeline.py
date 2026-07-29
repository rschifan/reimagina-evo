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
    URBAN_FORM_BLOCK_COMPOSITE,
    seed_catalog,
)
from reimagina.orchestration.intake import evaluate_scenario
from reimagina.orchestration.synchronous import SynchronousOrchestrator
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

    # Edit block_a only. Both direct indicators read geometry, and the composite
    # is pulled in downstream of them — the shipped DAG's only two-wave path.
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
        URBAN_FORM_BLOCK_COMPOSITE.slug,
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


def test_synchronous_orchestrator_keeps_per_unit_state(conn):
    block_a, _block_b = _seed_two_blocks(conn)

    # The reference implementation deliberately keeps state in memory. Hatchet
    # owns durable run state on this branch, so there is no shadow run table.
    orch = SynchronousOrchestrator()
    handle = orch.submit_run(
        conn,
        changed_attributes={"geometry"},
        edited_features={block_a},
        code_version="test",
    )
    assert handle.status is not None
    assert handle.status.status == "succeeded"

    # The same explicit synchronous instance can still expose its unit verdict.
    persisted = orch.get_run(conn, handle.run_id)
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert {u.indicator for u in persisted.units} == {
        URBAN_FORM_BLOCK_COMPACTNESS.slug,
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
        URBAN_FORM_BLOCK_COMPOSITE.slug,
    }
    assert all(u.status == "succeeded" for u in persisted.units)
def test_a_run_stamps_its_id_on_every_job_run_it_opens(conn):
    """The ambient run must survive the whole executor → runner → compute chain.

    ``current_run`` is set by the executor and read four frames down, inside the
    job_run lifecycle. Nothing in between mentions it, so only an end-to-end
    check proves the plumbing is connected — and without it, cancel cannot find
    a single backend to stop (r031).
    """
    block_a, _block_b = _seed_two_blocks(conn)

    handle = SynchronousOrchestrator().submit_run(
        conn,
        changed_attributes={"geometry"},
        edited_features={block_a},
        code_version="test",
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(backend_pid) FROM jobs_runtime.job_run "
            "WHERE run_id = %s",
            (handle.run_id,),
        )
        rows, with_pid = cur.fetchone()

    # One job_run per unit that actually computed, each carrying a pid.
    assert rows == len(handle.status.units)
    assert with_pid == rows
