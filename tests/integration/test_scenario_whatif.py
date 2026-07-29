"""End-to-end what-if: a scenario override changes an indicator's output.

The first test where a scenario is not just a tag — an override to the `floors`
attribute recomputes planned_floors to the new value, while the baseline keeps
the base data. Proves the effective base⊕override resolution + the impact
engine's attribute-keyed dirty-set through the full orchestrator.
"""

from __future__ import annotations

from uuid import uuid4

from reimagina.data import feature as feat
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_PLANNED_FLOORS, seed_catalog
from reimagina.orchestration.intake import evaluate_scenario
from reimagina.orchestration.synchronous import SynchronousOrchestrator
from reimagina.scenarios.overrides import apply_override
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

_SLUG = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
_WKT = (
    "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
    "-99.140 19.433,-99.140 19.430))"
)


def test_scenario_override_changes_planned_floors(conn):
    seed_catalog(conn)  # includes planned_floors
    source_id = _register_source(
        conn,
        name="whatif_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    block_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref="wf-blk-1",
        geometry_wkt=_WKT,
        attributes={"floors": 3},
    )

    orch = SynchronousOrchestrator()
    repo = backend_for_connection(conn).indicator_result_repo()

    # Baseline: a floors edit recomputes planned_floors to the base value 3.
    orch.submit_run(
        conn,
        changed_attributes={"floors"},
        edited_features={block_id},
        code_version="test",
    )
    base = repo.get_current(feature_id=block_id, indicator_slug=_SLUG)
    assert base is not None
    assert base["value_numeric"] == 3.0

    # What-if: scenario S overrides floors=10 → planned_floors(S) = 10.
    scenario = str(uuid4())
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=block_id,
        attribute_key="floors",
        value_numeric=10.0,
    )
    orch.submit_run(
        conn,
        changed_attributes={"floors"},
        edited_features={block_id},
        scenario_id=scenario,
        code_version="test",
    )

    scenario_result = repo.get_current(
        feature_id=block_id, indicator_slug=_SLUG, scenario_id=scenario
    )
    baseline_after = repo.get_current(feature_id=block_id, indicator_slug=_SLUG)
    assert scenario_result is not None
    assert scenario_result["value_numeric"] == 10.0  # the what-if changed it
    assert baseline_after["value_numeric"] == 3.0  # baseline untouched


def test_scenario_run_derives_change_set_from_overrides(conn):
    """A scenario run with NO explicit change-set derives it from the overrides.

    The override rows are the diff — the caller shouldn't have to re-declare
    what changed. This closes the silent-noop hole where a wrong/omitted
    changed_attributes yielded a green run that recomputed nothing.
    """
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="derive_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    block_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref="drv-blk-1",
        geometry_wkt=_WKT,
        attributes={"floors": 3},
    )

    scenario = str(uuid4())
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=block_id,
        attribute_key="floors",
        value_numeric=10.0,
    )

    # No changed_attributes / edited_features — they are derived from the override.
    report = evaluate_scenario(
        conn,
        changed_attributes=[],
        edited_features=[],
        scenario_id=scenario,
        code_version="test",
    )

    assert _SLUG in {o.indicator for o in report.outcomes}  # it actually ran
    repo = backend_for_connection(conn).indicator_result_repo()
    scenario_result = repo.get_current(
        feature_id=block_id, indicator_slug=_SLUG, scenario_id=scenario
    )
    assert scenario_result is not None
    assert scenario_result["value_numeric"] == 10.0  # derived + recomputed


def test_synchronous_orchestrator_also_derives_the_change_set(conn):
    """The port's reference implementation honours overrides too.

    It used to call ``compute_dirty_set`` directly, skipping the change-set
    resolution that ``evaluate_scenario`` and the async orchestrator both did —
    so submitting a scenario with no explicit change-set through the port
    silently recomputed nothing. Routing it through the shared prelude fixed
    that; this pins it.
    """
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="sync_derive_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    block_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref="sync-drv-blk-1",
        geometry_wkt=_WKT,
        attributes={"floors": 3},
    )

    scenario = str(uuid4())
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=block_id,
        attribute_key="floors",
        value_numeric=10.0,
    )

    handle = SynchronousOrchestrator().submit_run(
        conn,
        changed_attributes=[],
        edited_features=[],
        scenario_id=scenario,
        code_version="test",
    )

    assert handle.status is not None
    assert handle.status.status != "noop", "the override should have been derived"
    assert _SLUG in {u.indicator for u in handle.status.units}
    repo = backend_for_connection(conn).indicator_result_repo()
    result = repo.get_current(
        feature_id=block_id, indicator_slug=_SLUG, scenario_id=scenario
    )
    assert result is not None
    assert result["value_numeric"] == 10.0


def test_override_and_outbox_record_are_one_transaction(conn):
    """A committed scenario edit is never silently lost while Hatchet is down."""
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="outbox_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    feature_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref="outbox-blk-1",
        geometry_wkt=_WKT,
    )
    scenario = str(uuid4())
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=feature_id,
        attribute_key="floors",
        value_numeric=4.0,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scenario_id::text, changed_attributes, edited_features::text[]
            FROM jobs_runtime.orchestration_outbox
            WHERE scenario_id = %s::uuid
            """,
            (scenario,),
        )
        row = cur.fetchone()

    assert row == (scenario, ["floors"], [feature_id])

    from reimagina.orchestration.outbox import claim_pending, mark_delivered

    claimed = claim_pending(conn)
    event = next(item for item in claimed if item.scenario_id == scenario)
    assert event.changed_attributes == ("floors",)
    mark_delivered(conn, event.id, event.claim_token)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT delivered_at IS NOT NULL FROM jobs_runtime.orchestration_outbox WHERE id = %s::uuid",
            (event.id,),
        )
        assert cur.fetchone() == (True,)
