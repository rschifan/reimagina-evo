"""Client-only submission must not launch a DBOS executor in the API process."""

from __future__ import annotations

from reimagina.orchestration.impact import DirtyUnit
from reimagina.orchestration.plan import RunPlan


class _VersionCursor:
    """Answers the one query `dedup` makes: closure fingerprints per asset.

    `submit_run` grew a read of `serving_geo.asset_expected_version_v` when
    deduplication landed, so a bare `object()` is no longer a usable stand-in
    for a connection here. Returning a row for every key asked for is what
    makes the id *computable*; returning none is the fail-open path, and
    `test_dedup_ids.py` covers that.
    """

    def __init__(self, versions: dict[tuple[str, str], str]) -> None:
        self._versions = versions
        self._rows: list[tuple[str, str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        keys = params[0] if params else ()
        self._rows = [
            (key, part, self._versions.get((key, part), f"version-of-{key}"))
            for key, part in keys
        ]

    def fetchall(self):
        return self._rows


class _Conn:
    """A connection that can answer the asset-version query and nothing else."""

    def __init__(self, versions: dict[tuple[str, str], str] | None = None) -> None:
        self._versions = versions or {}

    def cursor(self):
        return _VersionCursor(self._versions)


def test_client_mode_enqueues_the_named_parent_workflow(monkeypatch):
    from reimagina.orchestration import dbos_app, dbos_orchestrator

    submitted: dict[str, object] = {}

    class Client:
        def enqueue(self, options, *args):
            submitted["options"] = options
            submitted["args"] = args

    monkeypatch.setattr(
        dbos_orchestrator,
        "plan_run",
        lambda *args, **kwargs: RunPlan(
            units=[DirtyUnit("derived", frozenset({"feature-1"}))],
            params={"code_version": "test", "scenario_id": None, "aoi_id": None},
        ),
    )
    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: Client())
    monkeypatch.setattr(dbos_app, "assert_same_domain_database", lambda conn: None)
    # The plan is persisted to `jobs_runtime.run_plan` on its own connection
    # before the workflow is enqueued; this test has no database.
    monkeypatch.setattr(dbos_orchestrator, "_persist_plan", lambda units: "plan-1")
    monkeypatch.setattr(
        dbos_app,
        "launch",
        lambda: (_ for _ in ()).throw(AssertionError("client launched an executor")),
    )

    handle = dbos_orchestrator.DBOSOrchestrator(
        registry=(), execution_mode="client"
    ).submit_run(
        _Conn(),
        changed_attributes={"geometry"},
        edited_features={"feature-1"},
        code_version="test",
    )

    assert submitted["options"]["workflow_name"] == "run_workflow"
    assert submitted["options"]["queue_name"] == dbos_app.RUNS_QUEUE
    assert submitted["options"]["workflow_id"] == handle.run_id
    assert submitted["args"][0] == handle.run_id


def test_the_workflow_input_carries_a_plan_handle_not_the_feature_ids(monkeypatch):
    """The payload must stay flat in the size of the city.

    Feature ids used to travel inside the workflow input, one per feature per
    dirty indicator — 258,675 of them for a full CDMX recompute. They live in
    `jobs_runtime.run_plan` now and the input carries a handle. This track never
    hit a hard ceiling because its input goes to Postgres rather than across a
    gRPC message, but it carried the same growth.
    """
    from reimagina.orchestration import dbos_app, dbos_orchestrator

    submitted: dict[str, object] = {}

    class Client:
        def enqueue(self, options, *args):
            submitted["args"] = args

    monkeypatch.setattr(
        dbos_orchestrator,
        "plan_run",
        lambda *args, **kwargs: RunPlan(
            units=[DirtyUnit("derived", frozenset({"f-1", "f-2", "f-3"}))],
            params={"code_version": "test", "scenario_id": None, "aoi_id": None},
        ),
    )
    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: Client())
    monkeypatch.setattr(dbos_app, "assert_same_domain_database", lambda conn: None)
    monkeypatch.setattr(dbos_orchestrator, "_persist_plan", lambda units: "plan-1")

    dbos_orchestrator.DBOSOrchestrator(registry=(), execution_mode="client").submit_run(
        _Conn(),
        changed_attributes={"geometry"},
        edited_features={"f-1"},
        code_version="test",
    )

    units, params = submitted["args"][1], submitted["args"][2]
    assert params["plan_id"] == "plan-1"
    assert [u["features"] for u in units] == [[]]
    # The slugs and the DAG edges stay: they set step positions, and spike 2
    # showed that re-deriving those misattributes one indicator's result.
    assert [u["slug"] for u in units] == ["derived"]


def test_the_enqueue_carries_a_deduplication_id(monkeypatch):
    """The queue, not a prior read, is what rejects a duplicate.

    Checking for an in-flight twin here and then enqueuing would be a read
    followed by a write with nothing holding between them: two concurrent
    submits of the same work would both see nothing and both enqueue. Handing
    the id to DBOS makes the uniqueness check and the insert the same
    operation.

    `return-existing` rather than the default `reject`: a resubmission is a
    caller asking for this work, and the useful answer is the run already
    doing it, not an exception.
    """
    from reimagina.orchestration import dbos_app, dbos_orchestrator

    submitted: dict[str, object] = {}

    class Client:
        def enqueue(self, options, *args):
            submitted["options"] = options

    monkeypatch.setattr(
        dbos_orchestrator,
        "plan_run",
        lambda *args, **kwargs: RunPlan(
            units=[DirtyUnit("derived", frozenset({"feature-1"}))],
            params={"code_version": "test", "scenario_id": None, "aoi_id": None},
        ),
    )
    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: Client())
    monkeypatch.setattr(dbos_app, "assert_same_domain_database", lambda conn: None)
    monkeypatch.setattr(dbos_orchestrator, "_persist_plan", lambda units: "plan-1")

    dbos_orchestrator.DBOSOrchestrator(registry=(), execution_mode="client").submit_run(
        _Conn(),
        changed_attributes={"geometry"},
        edited_features={"feature-1"},
        code_version="test",
    )

    options = submitted["options"]
    assert options["deduplication_id"].startswith("v1:")
    assert options["duplication_policy"] == "return-existing"


def test_a_collision_returns_the_run_already_doing_the_work(monkeypatch):
    """And says so, because the two outcomes are otherwise the same shape.

    `return-existing` discards the arguments just submitted and hands back a
    handle to the workflow holding the id. The id on that handle is the only
    signal that it happened — nothing raises, and the caller asked for a run
    and did get one.
    """
    from reimagina.orchestration import dbos_app, dbos_orchestrator

    class Handle:
        workflow_id = "the-run-already-running"

    class Client:
        def enqueue(self, options, *args):
            return Handle()

    monkeypatch.setattr(
        dbos_orchestrator,
        "plan_run",
        lambda *args, **kwargs: RunPlan(
            units=[DirtyUnit("derived", frozenset({"feature-1"}))],
            params={"code_version": "test", "scenario_id": None, "aoi_id": None},
        ),
    )
    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: Client())
    monkeypatch.setattr(dbos_app, "assert_same_domain_database", lambda conn: None)
    monkeypatch.setattr(dbos_orchestrator, "_persist_plan", lambda units: "plan-1")

    handle = dbos_orchestrator.DBOSOrchestrator(
        registry=(), execution_mode="client"
    ).submit_run(
        _Conn(),
        changed_attributes={"geometry"},
        edited_features={"feature-1"},
        code_version="test",
    )

    assert handle.run_id == "the-run-already-running"
    assert handle.deduplicated is True
