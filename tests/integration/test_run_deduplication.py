"""Submitting the same outstanding work twice must yield one run, not two.

A double-clicked button, a retried POST, an impatient operator: before this,
each minted a fresh run id and a second execution of work already in flight.
Both wrote the same ``indicator_result`` rows, one after the other, and the
second was pure waste.

Three behaviours matter and they pull against each other, which is why they
are tested together:

1. **Identical outstanding work collapses.** Otherwise the feature does
   nothing.
2. **Work whose inputs have moved does not collapse** — even when the request
   is byte-identical, which is exactly what a scenario edit looks like. This
   is the one that makes request-shape deduplication wrong.
3. **A finished run does not suppress a new one.** The deduplication id is
   released on every terminal transition, so the key is held only while the
   work is outstanding.

Run in ``client`` mode on purpose: a client enqueues and launches nothing, so
with no worker attached the run sits ``ENQUEUED`` and stays outstanding for as
long as the test needs. Racing a self-draining executor would make this
suite's result depend on how fast two blocks compute.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg2
import pytest

from reimagina.assets.registry import seed_assets
from reimagina.data import feature as feat
from reimagina.indicators.catalog import seed_catalog
from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator
from reimagina.scenarios.overrides import apply_overrides

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

_BLOCK = (
    "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
    "-99.140 19.433,-99.140 19.430))"
)


@pytest.fixture
def committed(migrated_db, monkeypatch):
    """A committed connection with catalog, assets and one block.

    Committed rather than the rollback ``conn`` fixture: the ops plane opens
    its own connections — ``_persist_plan`` and the DBOS client both do — and
    would not see uncommitted rows.
    """
    monkeypatch.setenv("DATABASE_URL_DIRECT", migrated_db)
    # Unique per test. `migrated_db` is session-scoped and this fixture
    # commits, so a fixed ref collides with the previous test's row on
    # (source_id, dataset_version, source_record_ref).
    tag = uuid4().hex[:8]
    connection = psycopg2.connect(migrated_db)
    try:
        # ⚠ Seeded with autocommit OFF, then switched on. A composite's
        # dependency rows are validated by a DEFERRABLE INITIALLY DEFERRED
        # trigger asserting its weights sum to 1.0; under autocommit each
        # INSERT commits alone and the first row of a 0.5/0.5 composite fails
        # the check by itself. `seed_catalog`'s own docstring says so, and
        # this fixture got it wrong first.
        connection.autocommit = False
        seed_catalog(connection)
        seed_assets(connection)
        connection.commit()
        connection.autocommit = True
        source_id = _register_source(
            connection,
            name=f"dedup_src_{tag}",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        ingestion = _start_run(connection, source_id)
        feature_id = _insert_feature(
            connection,
            type_id=feat.get_type_id(connection, "MEX", "block"),
            source_id=source_id,
            ingestion_run_id=ingestion,
            source_record_ref=f"dedup-block-{tag}",
            dataset_version="dedup-v1",
            geometry_wkt=_BLOCK,
            attributes={"floors": 3},
        )
        yield connection, feature_id
    finally:
        with connection.cursor() as cur:
            cur.execute("DELETE FROM jobs_runtime.job_unit")
            cur.execute("DELETE FROM jobs_runtime.orchestration_run")
            cur.execute("TRUNCATE jobs_runtime.run_plan")
            cur.execute("DELETE FROM curated_geo.scenario_override")
            # The feature and its source too: this fixture commits, so
            # anything it leaves behind is visible to every later test.
            cur.execute(
                "DELETE FROM curated_geo.feature WHERE source_id = %s", (source_id,)
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
                (source_id,),
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
                (source_id,),
            )
        connection.close()


def _orchestrator() -> DBOSOrchestrator:
    # Explicit rather than from the environment: this suite's whole premise is
    # that nothing drains the queue.
    return DBOSOrchestrator(execution_mode="client")


def _request(feature_id: str, **overrides) -> dict:
    request = {
        "changed_attributes": {"floors"},
        "edited_features": {str(feature_id)},
        "code_version": "dedup-suite",
    }
    request.update(overrides)
    return request


def test_resubmitting_outstanding_work_returns_the_same_run(committed):
    conn, feature_id = committed
    orchestrator = _orchestrator()

    first = orchestrator.submit_run(conn, **_request(feature_id))
    if first.status is not None:  # pragma: no cover - a no-op plan proves nothing
        pytest.skip("the plan was a no-op, so there is no run to deduplicate")
    second = orchestrator.submit_run(conn, **_request(feature_id))

    assert second.run_id == first.run_id
    assert second.deduplicated is True
    assert first.deduplicated is False, "the first submission invented a collision"


def test_the_caller_can_tell_the_two_outcomes_apart(committed):
    """`deduplicated` is the only signal; nothing raises and a run comes back.

    Without it the API answers a resubmission exactly as it answers a fresh
    one, and an operator watching a run id they have seen before cannot tell
    whether their click did anything.
    """
    conn, feature_id = committed
    orchestrator = _orchestrator()

    orchestrator.submit_run(conn, **_request(feature_id))
    second = orchestrator.submit_run(conn, **_request(feature_id))

    assert second.deduplicated is True
    assert second.status is None, "a deduplicated handle must not invent a status"


def test_an_edited_override_is_not_the_same_work(committed):
    """The case a request-shaped id gets wrong.

    The request does not change: same attribute, same feature, same scenario.
    Only the value changes. An id built from the request alone would hand the
    operator back the run computing the numbers they had just replaced.
    """
    conn, feature_id = committed
    orchestrator = _orchestrator()
    scenario_id = "11111111-2222-3333-4444-555555555555"

    apply_overrides(
        conn,
        scenario_id=scenario_id,
        edits=[
            {
                "feature_id": str(feature_id),
                "attribute_key": "floors",
                "value_numeric": 8.0,
            }
        ],
    )
    request = _request(feature_id, scenario_id=scenario_id)
    first = orchestrator.submit_run(conn, **request)

    apply_overrides(
        conn,
        scenario_id=scenario_id,
        edits=[
            {
                "feature_id": str(feature_id),
                "attribute_key": "floors",
                "value_numeric": 12.0,
            }
        ],
    )
    second = orchestrator.submit_run(conn, **request)

    assert second.run_id != first.run_id, (
        "the override moved but the run did not — the resubmission was "
        "answered with a run computing the replaced values"
    )
    assert second.deduplicated is False

    # And with nothing further changed it collapses again, so the previous
    # assertion is about the edit rather than about dedup never working here.
    third = orchestrator.submit_run(conn, **request)
    assert third.run_id == second.run_id
    assert third.deduplicated is True


def test_a_terminal_run_does_not_suppress_a_new_one(committed):
    """The id is released on every terminal transition, and must be.

    Inputs can change between two runs of the same request in ways no request
    field records. Holding the key past completion would mean an edit followed
    by a resubmission is answered with the run that predates it, forever.
    """
    conn, feature_id = committed
    orchestrator = _orchestrator()

    first = orchestrator.submit_run(conn, **_request(feature_id))
    assert orchestrator.cancel_run(conn, first.run_id) is True

    second = orchestrator.submit_run(conn, **_request(feature_id))
    assert second.run_id != first.run_id
    assert second.deduplicated is False


def test_the_test_suite_is_not_silently_failing_open(committed):
    """Guards every assertion above.

    `deduplication_id` returns None — disabling dedup entirely — when a
    planned unit has no asset fingerprint. On a database whose asset graph was
    not seeded, every test here would pass by never deduplicating anything,
    and `test_an_edited_override_is_not_the_same_work` would pass for the
    wrong reason.
    """
    from reimagina.orchestration.dedup import deduplication_id

    assert os.environ["DATABASE_URL_DIRECT"], "the suite needs a direct DSN"
    assert (
        deduplication_id(
            committed[0],
            unit_slugs=["urban_form_block_planned_floors"],
            changed_attributes={"floors"},
            edited_features={str(committed[1])},
            scenario_id=None,
            aoi_id=None,
            code_version="dedup-suite",
        )
        is not None
    ), "the asset graph is unseeded, so nothing in this file deduplicates"
