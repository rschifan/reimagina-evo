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
