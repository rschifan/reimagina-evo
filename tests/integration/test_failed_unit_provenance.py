"""A failed unit must still leave a ``job_run`` row, against a real database.

This is the regression the mocked unit tests cannot express. The unit runner
opens its ``job_run`` row on the connection doing the compute, and this track's
executor rolls that connection back when the unit raises — so the ``failed`` row
the runner wrote goes with the half-written results, and the unit that burned
minutes and died leaves nothing behind. ``job_run`` is the domain provenance
record (F8) and the per-run timing query in ``OPS_PLANE_RUNBOOK.md`` joins on
it; a failure with no row is a failure nothing can see.

Driven through ``dbos_app.s_compute`` — the real call site, with a real
rollback — rather than through the helper, because the helper existing is not
the property that was lost. Deliberately no engine: a step invoked outside a
workflow runs its own body, which is the code under test.
"""

from __future__ import annotations

import os
from contextlib import closing
from uuid import uuid4

import psycopg2
import pytest

from reimagina.jobs._lifecycle import job_run
from reimagina.orchestration import dbos_app
from reimagina.storage import backend_for_connection

SLUG = "urban_form_block_building_density"
CODE_VERSION = "provenance-test"


def _doomed_runner(conn, *, features, scenario_id, code_version, aoi_id):
    """Shaped like a real runner: opens its ``job_run`` row, then dies.

    Opening the row first is the whole point — it is what the executor's
    rollback erases. A poison that raised before ``job_run`` would leave nothing
    to lose and would pass against the broken code.
    """
    with job_run(
        backend_for_connection(conn),
        model_family="urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
    ):
        raise RuntimeError("poisoned by the provenance test")


def _healthy_runner(conn, *, features, scenario_id, code_version, aoi_id):
    with job_run(
        backend_for_connection(conn),
        model_family="urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
    ):
        pass


def _job_runs(dsn: str, run_id: str) -> list[tuple]:
    with closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message, model_family "
            "FROM jobs_runtime.job_run WHERE run_id = %s",
            (run_id,),
        )
        return cur.fetchall()


@pytest.fixture
def run_id(migrated_db, monkeypatch):
    """A run id to attribute rows to, cleaned up afterwards.

    Not the rollback ``conn`` fixture: the provenance write is on a connection
    of its own and commits, which is exactly the property under test, so nothing
    a test transaction rolls back could observe it.
    """
    monkeypatch.setenv("DATABASE_URL_DIRECT", migrated_db)
    rid = str(uuid4())
    yield rid
    with closing(psycopg2.connect(migrated_db)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs_runtime.job_run WHERE run_id = %s", (rid,))


def test_a_failed_unit_leaves_a_failed_job_run_for_its_run(run_id, monkeypatch):
    monkeypatch.setattr(dbos_app, "default_runners", lambda: {SLUG: _doomed_runner})

    with pytest.raises(RuntimeError):
        dbos_app.s_compute(run_id, SLUG, [], {"code_version": CODE_VERSION})

    rows = _job_runs(os.environ["DATABASE_URL_DIRECT"], run_id)
    # Exactly one: zero is the regression (rolled back with the compute), two
    # would mean a surviving in-transaction row double-counting the failure.
    assert len(rows) == 1, f"expected one job_run for the failed unit, got {rows}"
    status, error, model_family = rows[0]
    assert status == "failed"
    assert "poisoned by the provenance test" in error
    # Resolved from the catalog, so the replacement row groups with the one lost.
    assert model_family == "urban_form"


def test_a_succeeded_unit_still_writes_exactly_one_row(run_id, monkeypatch):
    """The failure path must not have taught the success path to double-write."""
    monkeypatch.setattr(dbos_app, "default_runners", lambda: {SLUG: _healthy_runner})

    dbos_app.s_compute(run_id, SLUG, [], {"code_version": CODE_VERSION})

    rows = _job_runs(os.environ["DATABASE_URL_DIRECT"], run_id)
    assert [r[0] for r in rows] == ["succeeded"]
