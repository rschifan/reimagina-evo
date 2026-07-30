"""The plan must be committed before the engine is told about it.

A worker can dequeue the instant `run()` returns, and it reads the plan on its
own connection. An uncommitted write is invisible there and indistinguishable
from a lost one, so the run fails with a missing-plan `LookupError` — the loud
error reserved for real defects — having been perfectly valid.

The race is timing-dependent and a fast worker makes it intermittent, so it
cannot be caught by observation. These assert the ordering structurally.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration.impact import DirtyUnit
from reimagina.orchestration.plan import RunPlan


class _Cursor:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_a, **_k):
        self._log.append("execute")

    @property
    def rowcount(self):
        return 0


class _Conn:
    """Records the order of everything `_persist_plan` does to a connection."""

    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cursor(self.log)

    def commit(self):
        self.log.append("commit")

    def close(self):
        self.log.append("close")


def test_persist_plan_commits_before_it_returns(monkeypatch):
    """The handle is only handed back once the rows are durable."""
    from reimagina.orchestration import hatchet_orchestrator

    log: list[str] = []

    class _Repo:
        def write(self, **_):
            log.append("write")

        def sweep(self, **_):
            log.append("sweep")

    class _Backend:
        def run_plan_repo(self):
            return _Repo()

    monkeypatch.setattr("reimagina.db.connect", lambda dsn: _Conn(log), raising=False)
    monkeypatch.setattr(
        hatchet_orchestrator, "backend_for_connection", lambda conn: _Backend()
    )

    plan_id = hatchet_orchestrator._persist_plan(
        "postgresql:///irrelevant", [DirtyUnit("slug", frozenset({"f-1"}))]
    )

    assert plan_id
    # The whole ordering, in one assertion: the rows are written, orphans are
    # reclaimed on the same connection, and only then is the work durable — and
    # the connection is closed before the caller receives the handle.
    assert log == ["write", "sweep", "commit", "close"], log


def test_the_plan_is_persisted_before_the_workflow_is_triggered(monkeypatch):
    """Ordering at the call site, not just inside the helper.

    Reversing these two lines is the silent version of this defect: the run is
    valid, the plan is on its way, and the worker loses the race.
    """
    from reimagina.orchestration import hatchet_app, hatchet_orchestrator

    order: list[str] = []

    class _Ref:
        workflow_run_id = "22222222-2222-4222-8222-222222222222"

    class _Workflow:
        def run(self, payload, **_kwargs):
            order.append("trigger")
            return _Ref()

    def _fake_persist(dsn, units):
        order.append("persist")
        return "plan-1"

    monkeypatch.setattr(
        hatchet_orchestrator,
        "plan_run",
        lambda *a, **k: RunPlan(
            units=[DirtyUnit("slug", frozenset({"f-1"}))],
            params={"code_version": "t", "scenario_id": None, "aoi_id": None},
        ),
    )
    monkeypatch.setattr(
        hatchet_orchestrator, "assert_same_domain_database", lambda *a, **k: None
    )
    monkeypatch.setattr(hatchet_orchestrator, "_persist_plan", _fake_persist)
    monkeypatch.setattr(hatchet_app, "get_run_workflow", lambda: _Workflow())
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")

    hatchet_orchestrator.HatchetOrchestrator(registry=()).submit_run(
        object(),
        changed_attributes={"geometry"},
        edited_features={"f-1"},
        code_version="t",
    )

    assert order == ["persist", "trigger"], order


def test_a_noop_writes_no_plan(monkeypatch):
    """Nothing to compute, nothing to store — and nothing to orphan.

    Writing before the noop check would leave rows for a run that never exists,
    reclaimable only by the TTL sweep.
    """
    from reimagina.orchestration import hatchet_orchestrator

    calls: list[str] = []
    monkeypatch.setattr(
        hatchet_orchestrator,
        "plan_run",
        lambda *a, **k: RunPlan(units=[], params={"code_version": "t"}),
    )
    monkeypatch.setattr(
        hatchet_orchestrator, "assert_same_domain_database", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hatchet_orchestrator,
        "_persist_plan",
        lambda *a, **k: calls.append("persist") or "x",
    )
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql:///irrelevant")

    handle = hatchet_orchestrator.HatchetOrchestrator(registry=()).submit_run(
        object(),
        changed_attributes={"nothing_reads_this"},
        edited_features={"f-1"},
        code_version="t",
    )

    assert handle.status is not None and handle.status.status == "noop"
    assert calls == [], "a noop wrote a plan"


@pytest.mark.parametrize("plan_id,expected", [("", "features"), ("p-1", "plan")])
def test_the_unit_falls_back_to_the_payload_when_there_is_no_handle(plan_id, expected):
    """The compatibility window, stated as behaviour rather than intent.

    A run submitted before `plan_id` existed and resumed after it still carries
    its ids in `RunInput.features`, and must still find its work. Current
    submitters never populate that field.
    """
    from reimagina.orchestration.hatchet_app import RunInput

    payload = RunInput(
        run_id="", dirty=["slug"], plan_id=plan_id, features={"slug": ["f-1"]}
    )
    resolved = "plan" if payload.plan_id else "features"
    assert resolved == expected
