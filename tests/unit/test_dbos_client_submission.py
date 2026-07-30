"""Client-only submission must not launch a DBOS executor in the API process."""

from __future__ import annotations

from reimagina.orchestration.impact import DirtyUnit
from reimagina.orchestration.plan import RunPlan


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
        object(),
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
        object(),
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
