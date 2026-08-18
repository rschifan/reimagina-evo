"""Contract tests for the intake API — POST /jobs enqueues, GET /jobs reads.

Mocked: no Postgres and no queue, so these run in the unit+contract CI job. The
real enqueue→worker→result path is covered by the async integration test.

The fake is installed over ``factory.get_orchestrator``, not over an engine
class: the route names only the port, so the factory is the single seam an
ops-plane swap moves — and these tests keep working across such a swap.
"""

from __future__ import annotations

import contextlib
import json

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from reimagina.orchestration.port import (
    RunEvent,
    RunHandle,
    RunStatus,
    UnitStatusView,
)


def _install(monkeypatch, orchestrator):
    """Make every ``get_orchestrator(...)`` in the route return this fake."""
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: orchestrator,
    )


def test_post_jobs_enqueues_and_returns_202(monkeypatch):
    @contextlib.contextmanager
    def _fake_connect(dsn=None):
        yield object()

    monkeypatch.setattr("reimagina.db.connect", _fake_connect)

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            return RunHandle(run_id="run-123", status=None)

    _install(monkeypatch, _FakeOrchestrator())

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

    _install(monkeypatch, _FakeOrchestrator())

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

    _install(monkeypatch, _FakeOrchestrator())

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

    _install(monkeypatch, _FakeOrchestrator())

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.post(
            "/jobs",
            json={"changed_attributes": ["not_read_by_anything"]},
        )

    assert resp.status_code == 202
    assert resp.json() == {"run_id": None, "status": "noop"}


def test_events_streams_the_orchestrators_progress(monkeypatch):
    """The SSE route serves whatever the port yields, over a real ASGI response.

    ``subscribe_progress`` is an *async* iterator (the service engines' streams
    are), so the route generator became async too — this pins that
    StreamingResponse actually drives it, which no unit test of the generator
    alone can show.
    """

    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return RunStatus(run_id=run_id, status="running", units=())

        async def subscribe_progress(self, run_id):
            yield RunEvent(run_id, None, "running")
            yield RunEvent(run_id, "urban_form_block_compactness", "succeeded")
            yield RunEvent(run_id, None, "succeeded")

    _install(monkeypatch, _FakeOrchestrator())

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        resp = client.get("/jobs/run-123/events")
        body = resp.text
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert [f["status"] for f in frames] == ["running", "succeeded", "succeeded"]


def test_events_404s_for_an_unknown_run(monkeypatch):
    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return None

    _install(monkeypatch, _FakeOrchestrator())

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        resp = client.get("/jobs/missing/events")
    app.dependency_overrides.clear()
    assert resp.status_code == 404
