"""The plan must be committed before the engine is told about it.

A worker can dequeue the instant `run()` returns, and it reads the plan on its
own connection. An uncommitted write is invisible there and indistinguishable
from a lost one, so the run fails with a missing-plan `LookupError` — the loud
error reserved for real defects — having been perfectly valid.

The race is timing-dependent and a fast worker makes it intermittent, so it
cannot be caught by observation. These assert the ordering structurally.
"""

from __future__ import annotations

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

        # Kept deliberately: if an age sweep is ever reintroduced on the submit
        # path it will show up in the log and fail the assertion below. Sweeping
        # here deleted live runs' plans — the horizon was 24 h against the
        # engine's 720 h retention, so anything replayed after a day lost its
        # input. Retention is a scheduled Postgres operation now.
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
    # The whole ordering, in one assertion: the rows are written and durable
    # before the caller receives the handle — and nothing is reclaimed on the
    # way through.
    assert log == ["write", "commit", "close"], log
    assert "sweep" not in log


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
    reclaimable only when the scheduled retention job next runs — which is now
    the full engine-retention horizon away, not 24 h.
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


def test_the_payload_carries_a_handle_and_no_inline_feature_ids():
    """The handle is the only way a unit finds its work.

    `RunInput.features` was the pre-`plan_id` shape, kept for one release so a
    run submitted before the change and resumed after it still resolved. That
    window is closed: carrying ids inline is what made a full-city run a 10 MB
    message the engine refused, and leaving the field on the model left a second
    path to the feature set that no submitter populated.
    """
    from reimagina.orchestration.hatchet_app import RunInput

    payload = RunInput(
        run_id="",
        dirty=["slug"],
        plan_id="p-1",
        concurrency_key="scope-under-test",
    )

    assert payload.plan_id == "p-1"
    assert not hasattr(payload, "features")
