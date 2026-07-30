"""A failed unit must still leave a ``job_run`` row, against a real database.

This is the regression the mocked unit tests cannot express. The unit runner
opens its ``job_run`` row on the connection doing the compute, and this track's
executor rolls that connection back when the unit raises — so the ``failed`` row
the runner wrote goes with the half-written results, and the unit that burned
minutes and died leaves nothing behind. ``job_run`` is the domain provenance
record (F8) and the per-run timing query in ``OPS_PLANE_RUNBOOK.md`` joins on
it; a failure with no row is a failure nothing can see.

Driven through the DAG's own unit task — the real call site, with a real
rollback — rather than through the helper, because the helper existing is not
the property that was lost. Deliberately no engine: the task's function is
called directly, which is the code under test. Importing ``hatchet_app`` at all
still needs ``HATCHET_CLIENT_TOKEN`` when it declares the static workflow. The
module itself imports without a token, but the SDK requires one to construct
the workflow decorator used by this direct task invocation.
"""

from __future__ import annotations

import os
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
from hatchet_sdk import NonRetryableException

from reimagina.jobs._lifecycle import job_run
from reimagina.orchestration.runners import default_runners
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


@pytest.fixture
def plan_id(migrated_db):
    """A real `run_plan` row, because the handle is now the only path.

    `RunInput.features` used to carry the ids inline and the unit fell back to
    it when `plan_id` was empty. That fallback is gone — one path to the feature
    set, not two — so a unit driven without a plan row fails on the read, which
    is the intended behaviour and not something to work around in a payload.
    """
    pid = str(uuid4())
    with closing(psycopg2.connect(migrated_db)) as conn:
        conn.autocommit = True
        backend_for_connection(conn).run_plan_repo().write(
            plan_id=pid, units={SLUG: []}
        )
    yield pid
    with closing(psycopg2.connect(migrated_db)) as conn:
        conn.autocommit = True
        backend_for_connection(conn).run_plan_repo().delete(plan_id=pid)


@pytest.fixture
def unit(run_id, plan_id, monkeypatch):
    """The shipped DAG's unit task, callable without an engine.

    ``nodes()`` returns the graph the module actually runs — built lazily and
    cached as one ``(workflow, nodes)`` pair — not a second ``build_dag()``
    shaped like it.

    ⚠ Not ``Task.mock_run``, which is the SDK's own no-engine harness. Its mock
    context answers ``workflow_run_id`` with the literal string
    ``'mock-workflow-run-id'``, and ``job_run.run_id`` is a ``uuid`` column — so
    every provenance write under it fails, best-effort swallows the error, and
    the test would report the regression it is meant to detect. Calling the
    task's own function with a context we control is what lets a real run id
    through.
    """
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.skip("HATCHET_CLIENT_TOKEN unset — workflow declaration needs a client")

    from reimagina.orchestration import hatchet_app

    def _install(runner):
        # ⚠ Override one slug, keep the rest. `build_dag` now resolves every
        # runner up front and refuses to declare a graph it cannot execute, so
        # a fake that returns only this slug makes the whole DAG unbuildable —
        # which is the guard working, not a test fixture being unlucky.
        monkeypatch.setattr(
            hatchet_app,
            "default_runners",
            lambda registry=None: {**default_runners(), SLUG: runner},
        )
        # ⚠ Drop the cached DAG so it rebuilds against the runner just
        # installed. The runner is now bound into the task closure at
        # declaration time rather than looked up per attempt, so a cached graph
        # keeps whichever runner the first test in the session installed — which
        # showed up as the healthy-unit test receiving the poisoned runner's
        # exception. Patching `default_runners` alone is no longer enough.
        monkeypatch.setattr(hatchet_app, "_dag", None)
        # `run_id` is the empty-string placeholder the submitter really sends:
        # the engine mints the id, so the task must read it off the context.
        payload = hatchet_app.RunInput(
            run_id="",
            dirty=[SLUG],
            plan_id=plan_id,
            code_version=CODE_VERSION,
            concurrency_key="scope-under-test",
        )
        # `is_cancelled=False`: the unit checks it before opening a connection
        # and declines to start when the run has already been cancelled.
        ctx = SimpleNamespace(
            workflow_run_id=run_id,
            attempt_number=1,
            log=lambda _msg: None,
            is_cancelled=False,
        )
        # `_fn`, not the `fn` property: same object, but the property is
        # deprecated for removal in SDK v2.0.0 and warns on every call.
        return lambda: hatchet_app.nodes()[SLUG]._fn(payload, ctx)

    return _install


def test_a_failed_unit_leaves_a_failed_job_run_for_its_run(unit, run_id):
    # ⚠ NonRetryableException, not RuntimeError. The task body classifies a
    # deterministic failure and re-raises it wrapped, so the runner's own
    # RuntimeError never reaches the caller — `pytest.raises(RuntimeError)` here
    # could not match, and only looked green because this module skips without a
    # token. The wrap is what tells the engine not to retry.
    with pytest.raises(NonRetryableException):
        unit(_doomed_runner)()

    rows = _job_runs(os.environ["DATABASE_URL_DIRECT"], run_id)
    # Exactly one: zero is the regression (rolled back with the compute), two
    # would mean a surviving in-transaction row double-counting the failure.
    # That it is found under `run_id` at all also pins `ctx.workflow_run_id`
    # over the empty `input.run_id` — reading the wrong one put `""` into a
    # uuid column and killed every unit.
    assert len(rows) == 1, f"expected one job_run for the failed unit, got {rows}"
    status, error, model_family = rows[0]
    assert status == "failed"
    assert "poisoned by the provenance test" in error
    # Resolved from the catalog, so the replacement row groups with the one lost.
    assert model_family == "urban_form"


def test_a_succeeded_unit_still_writes_exactly_one_row(unit, run_id):
    """The failure path must not have taught the success path to double-write."""
    unit(_healthy_runner)()

    rows = _job_runs(os.environ["DATABASE_URL_DIRECT"], run_id)
    assert [r[0] for r in rows] == ["succeeded"]


def test_a_unit_cancelled_before_it_starts_never_opens_a_transaction(
    monkeypatch, run_id
):
    """The cheap half of cancellation: decline to start.

    Cancellation is cooperative, and a unit still sitting in the queue is the
    one case where that is enough — it has no backend to hunt down, so checking
    `ctx.is_cancelled` before connecting saves the `pg_cancel_backend` round
    trip and leaves no half-open transaction behind. It does nothing for a unit
    already inside a blocking psycopg2 call; that is what `pg_cancel_backend`
    and `statement_timeout` are for.

    Non-retryable: a cancelled run is not a transient fault, and retrying it
    would re-enter the same guard twice more with backoff in between.
    """
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.skip("HATCHET_CLIENT_TOKEN unset — workflow declaration needs a client")

    from reimagina.orchestration import hatchet_app

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("a cancelled unit ran its compute")

    monkeypatch.setattr(
        hatchet_app,
        "default_runners",
        lambda registry=None: {**default_runners(), SLUG: _must_not_run},
    )

    payload = hatchet_app.RunInput(
        run_id="",
        dirty=[SLUG],
        code_version=CODE_VERSION,
        concurrency_key="scope-under-test",
    )
    ctx = SimpleNamespace(
        workflow_run_id=run_id,
        attempt_number=1,
        log=lambda _msg: None,
        is_cancelled=True,
    )

    with pytest.raises(NonRetryableException):
        hatchet_app.nodes()[SLUG]._fn(payload, ctx)

    # No connection opened means no provenance row, which is the observable
    # difference between "declined to start" and "started and was killed".
    assert _job_runs(os.environ["DATABASE_URL_DIRECT"], run_id) == []
