"""Cancellation is three mechanisms, and each one covers what the others cannot.

The engine stops *scheduling*. `pg_cancel_backend` stops a statement already
running. `statement_timeout` bounds one that nobody thought to cancel. None of
them can interrupt a worker thread sitting inside psycopg2's C extension, which
is why the first is not sufficient on its own and why the other two exist.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from reimagina.orchestration import hatchet_orchestrator as ho


class _Meta:
    def __init__(self, run_id: str) -> None:
        self.id = run_id


class _Row:
    def __init__(self, run_id: str) -> None:
        self.metadata = _Meta(run_id)


def _orchestrator_with(monkeypatch, *, listed, cancelled_ok):
    """Fake the engine listing; record which runs the cancel path touched."""
    from reimagina.orchestration import hatchet_app

    touched: list[str] = []

    class _Runs:
        def list_with_pagination(self, **_kwargs):
            return [_Row(r) for r in listed]

    monkeypatch.setattr(
        hatchet_app, "get_hatchet", lambda: SimpleNamespace(runs=_Runs())
    )

    orchestrator = ho.HatchetOrchestrator()

    def _cancel_run(_conn, run_id):
        touched.append(run_id)
        return run_id in cancelled_ok

    monkeypatch.setattr(orchestrator, "cancel_run", _cancel_run)
    return orchestrator, touched


def test_cancel_scenario_acts_on_exactly_the_runs_it_snapshotted(monkeypatch):
    """The set reported and the set acted on must be the same set.

    The bug this replaces listed ids, then called `bulk_cancel(filters=…)` — a
    server-side re-evaluation at a later instant — then killed backends only for
    the *listed* ids. A run submitted in between got its scheduling cancelled
    while its statement kept running.
    """
    orchestrator, touched = _orchestrator_with(
        monkeypatch, listed=["run-a", "run-b"], cancelled_ok={"run-a", "run-b"}
    )

    result = orchestrator.cancel_scenario(object(), "scenario-1")

    assert touched == ["run-a", "run-b"]
    assert result == ["run-a", "run-b"]


def test_cancel_scenario_reports_only_what_it_actually_stopped(monkeypatch):
    """A run that finished between the snapshot and its turn was not stopped."""
    orchestrator, touched = _orchestrator_with(
        monkeypatch, listed=["run-a", "already-done"], cancelled_ok={"run-a"}
    )

    result = orchestrator.cancel_scenario(object(), "scenario-1")

    assert touched == ["run-a", "already-done"]
    assert result == ["run-a"]


def test_cancel_scenario_goes_through_the_ordinary_per_run_path(monkeypatch):
    """Both halves per run, not a bulk call plus a separate backend sweep.

    `cancel_run` is the pair (engine cancel, then `pg_cancel_backend`). Reusing
    it is what keeps one cancellation path exercised by every caller.
    """
    orchestrator, touched = _orchestrator_with(
        monkeypatch, listed=["run-a"], cancelled_ok={"run-a"}
    )

    orchestrator.cancel_scenario(object(), "s", lookback=timedelta(days=1))

    assert touched == ["run-a"]


def test_an_empty_scenario_cancels_nothing(monkeypatch):
    orchestrator, touched = _orchestrator_with(
        monkeypatch, listed=[], cancelled_ok=set()
    )

    assert orchestrator.cancel_scenario(object(), "quiet-scenario") == []
    assert touched == []


def test_the_database_bound_matches_the_engine_bound():
    """Two knobs for one bound drift. The millisecond form is derived.

    ⚠ Not "the engine and the database give up at the same moment", which an
    earlier version of this docstring claimed. `statement_timeout` bounds each
    *statement* and `execution_timeout` bounds the *task*, so a unit issuing
    several long statements outlives both — see the note on
    `UNIT_EXECUTION_TIMEOUT_MS`. Deriving one from the other is still worth
    pinning: configured separately they drift, and a database bound longer than
    the engine's never fires at all.
    """
    from reimagina.orchestration import hatchet_app

    assert hatchet_app.UNIT_EXECUTION_TIMEOUT_MS == int(
        hatchet_app.UNIT_EXECUTION_TIMEOUT.total_seconds() * 1000
    )
    assert hatchet_app.UNIT_EXECUTION_TIMEOUT_MS > 0
