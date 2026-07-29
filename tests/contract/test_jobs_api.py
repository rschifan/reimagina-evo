"""Contract tests for the intake API — POST /jobs enqueues, GET /jobs reads.

Mocked: no Postgres and no queue, so these run in the unit+contract CI job. The
real enqueue→worker→result path is covered by the async integration test.

The fake is installed over ``factory.get_orchestrator``, not over an engine
class: the route names only the port, so the factory is the single seam an
ops-plane swap moves — and these tests keep working across such a swap.
"""

from __future__ import annotations

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

_RUN_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
"""A well-formed run id.

⚠ Not a convenience string like ``run-123``. The routes type ``run_id``
as ``uuid.UUID`` so a malformed id is rejected at the boundary with 422
rather than reaching the engine client — where it raises a validation
error carrying no HTTP status, which the orchestrator cannot tell from an
outage and reports as a retryable 503. These tests exercise the routes,
not that guard, so they need an id that gets past it."""


class _FakeConnection:
    def close(self) -> None:
        pass


def _install(monkeypatch, orchestrator):
    """Make every ``get_orchestrator(...)`` in the route return this fake."""
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: orchestrator,
    )


def test_post_jobs_enqueues_and_returns_202(monkeypatch):
    def _fake_connect(dsn=None):
        return _FakeConnection()

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
        resp = client.get(f"/jobs/{_RUN_ID}")
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
        resp = client.get(f"/jobs/{_RUN_ID}")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_post_jobs_noop_returns_null_run_id(monkeypatch):
    """An empty dirty-set persists no run, so the id must not be advertised.

    Returning one made a client following the documented poll loop 404 on
    GET /jobs/{run_id} immediately after a *successful* submit.
    """

    def _fake_connect(dsn=None):
        return _FakeConnection()

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


def test_post_jobs_rejects_removed_in_request_worker_mode():
    """``wait`` used to start a worker in the API request; that is unsafe."""
    with TestClient(create_app(initialise_pool=False)) as client:
        response = client.post("/jobs", json={"wait": True})

    assert response.status_code == 422


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
        resp = client.get(f"/jobs/{_RUN_ID}/events")
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
        resp = client.get(f"/jobs/{_RUN_ID}/events")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


# ── Boundary validation and stream failure ──────────────────────────────────


def test_a_malformed_run_id_is_rejected_at_the_boundary(monkeypatch):
    """422, not 503.

    ⚠ The regression this pins is subtle. `run_id` used to be `str`, so a
    malformed id reached the engine client, whose own length validation raises a
    pydantic error carrying no HTTP status — which `_is_not_found` could not
    distinguish from an outage, so the orchestrator raised "unavailable" and the
    caller got **503 with Retry-After**, told to retry a request that can never
    succeed. Typing the parameter deletes the whole special case.
    """

    class _FakeOrchestrator:
        def get_run(self, conn, run_id):  # pragma: no cover - must not be reached
            raise AssertionError("a malformed id must not reach the orchestrator")

    _install(monkeypatch, _FakeOrchestrator())

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        detail = client.get("/jobs/not-a-uuid")
        events = client.get("/jobs/not-a-uuid/events")
    app.dependency_overrides.clear()

    assert detail.status_code == 422
    assert events.status_code == 422


def test_wait_false_is_accepted_and_wait_true_is_not(monkeypatch):
    """`wait: false` asked for the only behaviour this endpoint still has.

    Removing the field while forbidding extras rejected it with 422 — breaking
    clients that merely spelled out the former default. Rejecting `true`, which
    used to run a worker inside the request, is the actual contract change.
    """

    def _fake_connect(dsn=None):
        return _FakeConnection()

    monkeypatch.setattr("reimagina.db.connect", _fake_connect)

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            return RunHandle(run_id=_RUN_ID, status=None)

    _install(monkeypatch, _FakeOrchestrator())

    body = {"changed_attributes": ["geometry"], "edited_features": ["b1"]}
    with TestClient(create_app(initialise_pool=False)) as client:
        accepted = client.post("/jobs", json={**body, "wait": False})
        rejected = client.post("/jobs", json={**body, "wait": True})

    assert accepted.status_code == 202
    assert rejected.status_code == 422


def test_an_outage_midstream_yields_one_error_frame_then_closes(monkeypatch):
    """The stream cannot become a 503 once its headers are on the wire.

    Starlette refuses to run a registered handler after the response has
    started, so an orchestrator failure mid-stream used to just stop the
    stream — indistinguishable, to a client, from a run that finished.
    """

    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return RunStatus(run_id=run_id, status="running", units=())

        async def subscribe_progress(self, run_id):
            yield RunEvent(run_id, "urban_form_block_compactness", "running")
            raise RuntimeError("engine went away")

    _install(monkeypatch, _FakeOrchestrator())

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as client:
        body = client.get(f"/jobs/{_RUN_ID}/events").text
    app.dependency_overrides.clear()

    assert body.count("event: error") == 1, body
    assert json.loads(
        body.split("event: error\n", 1)[1].splitlines()[0].removeprefix("data: ")
    ) == {"reason": "orchestrator_unavailable"}
    # The reason is a fixed token; the underlying message never reaches the wire.
    assert "engine went away" not in body
