"""Contract tests for the intake API — POST /jobs enqueues, GET /jobs reads.

Mocked: no Postgres or Procrastinate queue, so these run in the unit+contract CI
job. The real enqueue→worker→result path is covered by the async integration
test.
"""

from __future__ import annotations

import contextlib

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView


def test_post_jobs_enqueues_and_returns_202(monkeypatch):
    @contextlib.contextmanager
    def _fake_connect(dsn=None):
        yield object()

    monkeypatch.setattr("reimagina.db.connect", _fake_connect)

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            return RunHandle(run_id="run-123", status=None)

    monkeypatch.setattr(
        "reimagina.orchestration.procrastinate_orchestrator.ProcrastinateOrchestrator",
        _FakeOrchestrator,
    )

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.post(
            "/jobs",
            json={"changed_attributes": ["geometry"], "edited_features": ["b1"]},
        )
    assert resp.status_code == 202
    assert resp.json() == {"run_id": "run-123", "status": "queued"}


def test_get_jobs_returns_run_status(monkeypatch):
    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return RunStatus(
                run_id=run_id,
                status="succeeded",
                units=(UnitStatusView("urban_form_block_compactness", "succeeded"),),
            )

    monkeypatch.setattr(
        "reimagina.orchestration.procrastinate_orchestrator.ProcrastinateOrchestrator",
        _FakeOrchestrator,
    )

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        resp = client.get("/jobs/run-123")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["units"][0]["indicator"] == "urban_form_block_compactness"


def test_get_jobs_404_when_missing(monkeypatch):
    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return None

    monkeypatch.setattr(
        "reimagina.orchestration.procrastinate_orchestrator.ProcrastinateOrchestrator",
        _FakeOrchestrator,
    )

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        resp = client.get("/jobs/missing")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_post_jobs_noop_returns_null_run_id(monkeypatch):
    """An empty dirty-set persists no run, so the id must not be advertised.

    Returning one made a client following the documented poll loop 404 on
    GET /jobs/{run_id} immediately after a *successful* submit.
    """

    @contextlib.contextmanager
    def _fake_connect(dsn=None):
        yield object()

    monkeypatch.setattr("reimagina.db.connect", _fake_connect)

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            # The orchestrator still mints an id for correlation in its logs.
            return RunHandle(
                run_id="run-noop",
                status=RunStatus(run_id="run-noop", status="noop", units=()),
            )

    monkeypatch.setattr(
        "reimagina.orchestration.procrastinate_orchestrator.ProcrastinateOrchestrator",
        _FakeOrchestrator,
    )

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.post(
            "/jobs",
            json={"changed_attributes": ["not_read_by_anything"]},
        )

    assert resp.status_code == 202
    assert resp.json() == {"run_id": None, "status": "noop"}
