"""One dispatched child, three indicators — grouping through a real DBOS run.

The unit tests pin the *shape* of the workflow body against a recording double.
This pins the thing that actually matters to the collaborators' ask: submit an
attribute edit, and the engine dispatches one unit of work while the frontend
still sees every indicator complete individually.

Counting children is the proof. DBOS names a child ``<parent-id>-<n>``, so the
number of ``unit_workflow`` rows under a run in ``dbos.workflow_status`` is
exactly the number of units dispatched. Three job_unit rows beside one child is
grouping; three children is not.

Why this matters beyond tidiness: the workload benchmark measured per-unit
engine overhead at ~300 ms when units run in parallel and ~1.0–1.4 s when they
serialize — far more than these indicators spend computing. Collapsing three
units into one is the single largest saving available on this catalog.
"""

from __future__ import annotations

import time
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
)
from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator
from reimagina.orchestration.port import RUN_TERMINAL_STATES

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    seed_catalog_committed,
    start_run as _start_run,
)

_WKT = (
    "POLYGON((-99.140000 19.430000,-99.139859 19.430000,"
    "-99.139859 19.430134,-99.140000 19.430134,-99.140000 19.430000))"
)
_ATTRS = {"num_levels": 3, "unit_size": 60, "people_per_household": 2.3}
_EDIFICABILITY = {
    EDIFICABILITY_BLOCK_HOUSING_UNITS.slug,
    EDIFICABILITY_BLOCK_POPULATION.slug,
    EDIFICABILITY_BLOCK_DENSITY.slug,
}


@pytest.fixture
def dbos_env(migrated_db, monkeypatch):
    """A committed connection with the catalog and one attributed block.

    Committed rather than the rollback ``conn`` fixture: the ops plane opens its
    own connections and would not see uncommitted rows. Cleanup is surgical, and
    reaches into ``dbos.workflow_status`` too — runs accumulate there, and a
    suite that cleans only ``jobs_runtime`` leaves enough behind to make later
    tests behave differently than when run alone.
    """
    monkeypatch.setenv("DATABASE_URL_DIRECT", migrated_db)
    # Production default is client-only; `dev` is the explicit one-process mode.
    monkeypatch.setenv("REIMAGINA_DBOS_EXECUTION_MODE", "dev")

    try:
        psycopg2.connect(migrated_db).close()
    except psycopg2.OperationalError as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    conn = psycopg2.connect(migrated_db)
    conn.autocommit = True
    run_ids: list[str] = []
    source_id = None
    try:
        seed_catalog_committed(conn)
        source_id = _register_source(
            conn,
            name=f"grp_{uuid4().hex[:8]}",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        block_id = _insert_feature(
            conn,
            type_id=feat.get_type_id(conn, "MEX", "block"),
            source_id=source_id,
            ingestion_run_id=_start_run(conn, source_id),
            dataset_version="v1",
            source_record_ref=f"grp-blk-{uuid4().hex[:8]}",
            geometry_wkt=_WKT,
            attributes=_ATTRS,
        )
        yield conn, block_id, run_ids
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('dbos.workflow_status')")
            if cur.fetchone()[0] is not None:
                for run_id in run_ids:
                    cur.execute(
                        "DELETE FROM dbos.workflow_status "
                        "WHERE workflow_uuid = %s OR workflow_uuid LIKE %s",
                        (run_id, f"{run_id}-%"),
                    )
            for run_id in run_ids:
                cur.execute(
                    "DELETE FROM jobs_runtime.job_unit WHERE run_id = %s", (run_id,)
                )
                cur.execute(
                    "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s",
                    (run_id,),
                )
            if source_id is not None:
                cur.execute(
                    "DELETE FROM curated_geo.indicator_result WHERE feature_id IN "
                    "(SELECT id FROM curated_geo.feature WHERE source_id = %s)",
                    (source_id,),
                )
        conn.close()


def _await_terminal(orchestrator, conn, run_id: str, timeout_s: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = orchestrator.get_run(conn, run_id)
        if status is not None and status.status in RUN_TERMINAL_STATES:
            return status.status
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout_s}s")


def _children(conn, run_id: str) -> list[str]:
    """Child workflow ids under this run — one per dispatched unit."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_uuid FROM dbos.workflow_status "
            "WHERE workflow_uuid LIKE %s AND name LIKE %s "
            "ORDER BY workflow_uuid",
            (f"{run_id}-%", "%unit_workflow%"),
        )
        return [row[0] for row in cur.fetchall()]


def test_one_child_produces_three_indicators(dbos_env):
    conn, block_id, run_ids = dbos_env
    orchestrator = DBOSOrchestrator()

    handle = orchestrator.submit_run(
        conn,
        changed_attributes=["num_levels"],
        edited_features=[block_id],
        code_version="test",
    )
    run_ids.append(handle.run_id)
    final = _await_terminal(orchestrator, conn, handle.run_id)
    assert final == "succeeded", f"run ended {final}"

    status = orchestrator.get_run(conn, handle.run_id)
    slugs = {u.indicator for u in status.units}
    assert slugs == _EDIFICABILITY
    assert all(u.status == "succeeded" for u in status.units), status.units

    # The whole point: three indicators, one dispatched unit.
    children = _children(conn, handle.run_id)
    assert (
        len(children) == 1
    ), f"expected one grouped child, got {len(children)}: {children}"


def test_ungrouped_indicators_still_dispatch_one_child_each(dbos_env):
    """The control, without which the count above proves nothing.

    A `geometry` edit dirties compactness, building_density and their composite
    — three indicators in no run_group. If the same query returns three children
    here and one for the grouped case, the difference is grouping and not an
    artefact of how children are counted.
    """
    conn, block_id, run_ids = dbos_env
    orchestrator = DBOSOrchestrator()

    handle = orchestrator.submit_run(
        conn,
        changed_attributes=["geometry"],
        edited_features=[block_id],
        code_version="test",
    )
    run_ids.append(handle.run_id)
    _await_terminal(orchestrator, conn, handle.run_id)

    status = orchestrator.get_run(conn, handle.run_id)
    assert len(status.units) == 3, status.units
    children = _children(conn, handle.run_id)
    assert (
        len(children) == 3
    ), f"ungrouped indicators must dispatch one child each, got {children}"


def test_every_indicator_still_gets_its_own_row_and_result(dbos_env):
    """Grouping changes dispatch, never what the frontend can observe."""
    conn, block_id, run_ids = dbos_env
    orchestrator = DBOSOrchestrator()

    handle = orchestrator.submit_run(
        conn,
        changed_attributes=["num_levels"],
        edited_features=[block_id],
        code_version="test",
    )
    run_ids.append(handle.run_id)
    _await_terminal(orchestrator, conn, handle.run_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT indicator_slug, status FROM jobs_runtime.job_unit "
            "WHERE run_id = %s ORDER BY indicator_slug",
            (handle.run_id,),
        )
        units = cur.fetchall()
        cur.execute(
            "SELECT indicator_slug FROM curated_geo.indicator_result "
            "WHERE feature_id = %s::uuid AND status = 'current' "
            "AND indicator_slug = ANY(%s)",
            (block_id, sorted(_EDIFICABILITY)),
        )
        written = {row[0] for row in cur.fetchall()}

    assert {slug for slug, _ in units} == _EDIFICABILITY
    assert all(status == "succeeded" for _, status in units)
    # Each member wrote its own value — a group that computed once and wrote one
    # slug would pass the status checks above and still be wrong.
    assert written == _EDIFICABILITY
