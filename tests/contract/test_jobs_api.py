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


def _install(monkeypatch, orchestrator):
    """Make every ``get_orchestrator(...)`` in the route return this fake."""
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: orchestrator,
    )


class _FakeConn:
    """A stand-in shaped like what ``reimagina.db.connect`` actually returns.

    ⚠ A raw connection, **not** a context manager yielding one — which is what
    this fake used to be, and why nothing here could see that the routes leaked.
    psycopg2's ``__enter__``/``__exit__`` scope the *transaction*: entering and
    leaving commits or rolls back and leaves the socket open. Modelling that
    faithfully is what lets ``closed`` below be an assertion rather than a hope.
    """

    def __init__(self) -> None:
        self.closed = False
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True
        return False

    def close(self) -> None:
        self.closed = True

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _fake_connections(monkeypatch) -> list[_FakeConn]:
    """Hand every ``connect()`` in the route a fake, and keep them for inspection."""
    opened: list[_FakeConn] = []

    def _connect(dsn=None):
        conn = _FakeConn()
        opened.append(conn)
        return conn

    monkeypatch.setattr("reimagina.db.connect", _connect)
    return opened


def test_post_jobs_enqueues_and_returns_202(monkeypatch):
    opened = _fake_connections(monkeypatch)

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
    # The submit connection is committed *and* closed. Committed because the
    # enqueue must survive; closed because this route runs on every submit in a
    # long-lived process, and psycopg2's context manager does not close.
    assert [(c.committed, c.closed) for c in opened] == [(True, True)]


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

    _fake_connections(monkeypatch)

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
    opened = _fake_connections(monkeypatch)

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.get("/jobs/run-123/events")
        body = resp.text

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # ⚠ The route takes no `Depends(get_db_conn)`. FastAPI closes a yield
    # dependency only after the *response* completes, and a StreamingResponse
    # does not complete until its body iterator is exhausted — so a pooled
    # connection borrowed for the 404 check stayed borrowed for the whole
    # subscription. Its own connection instead, closed before streaming starts.
    assert [c.closed for c in opened] == [True]
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
    opened = _fake_connections(monkeypatch)

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.get("/jobs/missing/events")
    assert resp.status_code == 404
    # Closed on the 404 path too, where there is no stream to end and release it.
    assert [c.closed for c in opened] == [True]


# ── Ops dashboard read routes ────────────────────────────────────────────────
#
# The reader is faked at ``get_backend`` rather than at the SQL, for the same
# reason the orchestrator is faked at the factory: these assert the route's
# contract — binding, normalisation, status codes — and the SQL itself has its
# own tests against a real database in tests/integration/test_ops_run_reader.py.


class _FakeOpsReader:
    """Records what the route asked for, so the binding can be asserted."""

    def __init__(self, runs=None):
        self.runs = runs or []
        self.calls: list[dict] = []

    def list_runs(self, *, statuses=None, limit=50, offset=0):
        self.calls.append({"statuses": statuses, "limit": limit, "offset": offset})
        return self.runs

    def count_runs(self, *, statuses=None):
        return len(self.runs)

    def count_runs_by_status(self):
        return {"succeeded": 1}

    def get_run_row(self, run_id):
        return self.runs[0] if self.runs else None

    def list_units_detailed(self, run_id):
        return [
            {
                "indicator": "urban_form_block_compactness",
                "status": "succeeded",
                "detail": None,
                "queued_at": "2026-08-20T10:00:00+00:00",
                "started_at": "2026-08-20T10:00:01+00:00",
                "completed_at": "2026-08-20T10:00:09+00:00",
            }
        ]

    def list_executor_health(self):
        return [
            {
                "executor_id": "worker-1",
                "role": "worker",
                "app_version": None,
                "heartbeat_age_s": 4.2,
            }
        ]


def _run_row(**overrides):
    row = {
        "run_id": "06baed8c-a6c9-47c1-ab0c-cc38a77f260c",
        "status": "succeeded",
        "scenario_id": None,
        "aoi_id": None,
        "code_version": "test",
        "submitted_at": "2026-08-20T10:00:00+00:00",
        "completed_at": "2026-08-20T10:00:10+00:00",
        "units_total": 3,
        "units_terminal": 3,
        "units_succeeded": 3,
        "units_failed": 0,
        "units_running": 0,
        "units_last_terminal_at": "2026-08-20T10:00:09+00:00",
    }
    row.update(overrides)
    return row


def _app_with_reader(reader, monkeypatch):
    monkeypatch.setattr(
        "apps.api.routers.jobs.get_backend",
        lambda conn: type("_B", (), {"ops_run_reader": lambda self: reader})(),
    )
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])
    return app


def test_list_jobs_returns_the_whole_index_in_one_response(monkeypatch):
    reader = _FakeOpsReader([_run_row()])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    # One request per tick has to carry everything the index renders.
    assert body["runs"][0]["units_total"] == 3
    assert body["total"] == 1
    assert body["status_counts"] == {"succeeded": 1}
    assert body["executors"][0]["heartbeat_age_s"] == 4.2
    # The clock the page corrects against; without it every duration is the
    # browser's guess.
    assert body["server_now"]


def test_repeated_status_params_reach_the_reader(monkeypatch):
    """The regression ``Query(None)`` prevents: a silently ignored filter.

    With a bare ``list[RunStatus] | None`` annotation FastAPI reads this as a
    body parameter, binds None, and returns 200 with every run — a filter that
    looks like it worked.
    """
    reader = _FakeOpsReader([])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs?status=failed&status=running")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert reader.calls[0]["statuses"] == ["failed", "running"]


def test_absent_status_binds_none_not_empty_list(monkeypatch):
    """``[]`` matches no row in SQL, so "no filter" must not arrive as one."""
    reader = _FakeOpsReader([])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        client.get("/jobs")
    app.dependency_overrides.clear()

    assert reader.calls[0]["statuses"] is None


def test_unknown_status_is_a_422(monkeypatch):
    reader = _FakeOpsReader([])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs?status=definitely-not-a-status")
    app.dependency_overrides.clear()

    assert resp.status_code == 422


def test_limit_is_bounded(monkeypatch):
    """An unbounded limit on a two-second poll is a self-inflicted load test."""
    reader = _FakeOpsReader([])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        assert client.get("/jobs?limit=100000").status_code == 422
        assert client.get("/jobs?limit=0").status_code == 422
        assert client.get("/jobs?offset=-1").status_code == 422
    app.dependency_overrides.clear()


def test_run_detail_carries_unit_timings(monkeypatch):
    reader = _FakeOpsReader([_run_row()])
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c/detail")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    unit = resp.json()["units"][0]
    assert unit["started_at"] and unit["completed_at"]


def test_run_detail_falls_back_to_the_port_before_the_envelope_exists(monkeypatch):
    """The window between ``submit_run`` returning and the workflow's first step.

    ``orchestration_run`` is written by ``s_open_run``, so for a second or two a
    purely SQL answer is 404 — precisely when the user has just been navigated
    to the run they launched.
    """

    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return RunStatus(
                run_id=run_id,
                status="queued",
                units=(UnitStatusView("urban_form_block_compactness", "queued"),),
            )

    _install(monkeypatch, _FakeOrchestrator())
    reader = _FakeOpsReader([])  # no envelope row yet
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c/detail")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "queued"
    assert resp.json()["units"][0]["indicator"] == "urban_form_block_compactness"


def test_run_detail_404s_when_neither_plane_knows_the_run(monkeypatch):
    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return None

    _install(monkeypatch, _FakeOrchestrator())
    app = _app_with_reader(_FakeOpsReader([]), monkeypatch)

    with TestClient(app) as client:
        resp = client.get("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c/detail")
    app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── Impact preview ───────────────────────────────────────────────────────────


def _preview_app(monkeypatch, plan_units, raises=None):
    """Fake `plan_run` at the route's import site; no DB, no engine."""
    from reimagina.orchestration.plan import RunPlan

    def _plan_run(conn, **kwargs):
        if raises is not None:
            raise raises
        return RunPlan(
            units=[
                type("U", (), {"indicator": slug, "features": frozenset()})()
                for slug in plan_units
            ],
            params={"scenario_id": None, "aoi_id": None, "code_version": "test"},
        )

    monkeypatch.setattr("reimagina.orchestration.plan.plan_run", _plan_run)
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])
    return app


def test_preview_reports_what_a_submit_would_recompute(monkeypatch):
    """The dry run: derive the dirty set, persist nothing, enqueue nothing."""
    app = _preview_app(monkeypatch, ["edificability_block_density"])

    with TestClient(app) as client:
        resp = client.post("/jobs/preview", json={"changed_attributes": ["num_levels"]})
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["indicators"] == ["edificability_block_density"]
    assert body["count"] == 1
    assert body["noop"] is False
    assert "change-set" in body["reason"]


def test_preview_distinguishes_a_noop_from_a_failure(monkeypatch):
    """An empty dirty set is a correct answer and must read as one.

    This is the case an operator most needs named: the submit that follows
    would return `noop` with a null run id, and without a reason that looks
    like the engine is broken.
    """
    app = _preview_app(monkeypatch, [])

    with TestClient(app) as client:
        resp = client.post("/jobs/preview", json={})
    app.dependency_overrides.clear()

    body = resp.json()
    assert resp.status_code == 200
    assert body["count"] == 0
    assert body["noop"] is True
    assert "nothing is out of date" in body["reason"]


def test_preview_422s_an_unknown_indicator(monkeypatch):
    from reimagina.orchestration.plan import UnknownIndicators

    app = _preview_app(monkeypatch, [], raises=UnknownIndicators(["nope"]))

    with TestClient(app) as client:
        resp = client.post("/jobs/preview", json={"indicators": ["nope"]})
    app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]


def test_submit_forwards_named_indicators_to_the_port(monkeypatch):
    """`indicators` must reach `submit_run`, not be silently dropped."""
    _fake_connections(monkeypatch)
    seen: dict = {}

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            seen.update(kwargs)
            return RunHandle(run_id="run-9", status=None)

    _install(monkeypatch, _FakeOrchestrator())

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.post("/jobs", json={"indicators": ["livability_block_score"]})

    assert resp.status_code == 202
    assert seen["indicators"] == ["livability_block_score"]


def test_preview_resolves_a_changed_dataset(monkeypatch):
    """ "I added a street" must be answerable without naming an attribute."""
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])

    with TestClient(app) as client:
        resp = client.post(
            "/jobs/preview", json={"changed_datasets": ["global.road_network"]}
        )
    app.dependency_overrides.clear()

    body = resp.json()
    assert resp.status_code == 200
    assert body["noop"] is False
    assert "accessibility_block_school_network_m" in body["indicators"]
    # downstream of the accessibility indicators
    assert "livability_block_score" in body["indicators"]
    assert "downstream" in body["reason"]


def test_preview_says_so_when_nothing_reads_the_dataset(monkeypatch):
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])

    with TestClient(app) as client:
        resp = client.post("/jobs/preview", json={"changed_datasets": ["mex.lot"]})
    app.dependency_overrides.clear()

    body = resp.json()
    assert body["count"] == 0
    assert body["noop"] is True
    assert "no indicator declares an edge" in body["reason"]


def test_submitting_a_changed_dataset_scopes_the_run_the_same_way(monkeypatch):
    """The preview and the run must not be able to disagree."""
    _fake_connections(monkeypatch)
    seen: dict = {}

    class _FakeOrchestrator:
        def submit_run(self, conn, **kwargs):
            seen.update(kwargs)
            return RunHandle(run_id="run-ds", status=None)

    _install(monkeypatch, _FakeOrchestrator())

    with TestClient(create_app(initialise_pool=False)) as client:
        resp = client.post("/jobs", json={"changed_datasets": ["global.building"]})

    assert resp.status_code == 202
    assert "urban_form_block_building_density" in seen["indicators"]
    assert "urban_form_block_composite" in seen["indicators"]


# ── Deleting run history ─────────────────────────────────────────────────────


class _DeletingReader(_FakeOpsReader):
    def __init__(self, runs=None):
        super().__init__(runs)
        self.deleted: list[str] = []
        self.cleared = 0

    def delete_run(self, run_id):
        self.deleted.append(run_id)
        return True

    def delete_terminal_runs(self):
        self.cleared += 1
        return 4


def test_deleting_a_finished_run_removes_it(monkeypatch):
    reader = _DeletingReader([_run_row(status="succeeded")])
    _fake_connections(monkeypatch)
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.delete("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1}
    assert reader.deleted == ["06baed8c-a6c9-47c1-ab0c-cc38a77f260c"]


def test_deleting_a_live_run_is_refused(monkeypatch):
    """Its workflow still owns those rows — deleting mid-flight would have the
    remaining steps write into a run that no longer exists."""
    reader = _DeletingReader([_run_row(status="running", completed_at=None)])
    _fake_connections(monkeypatch)
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.delete("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c")
    app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert "cancel it first" in resp.json()["detail"]
    assert reader.deleted == []


def test_deleting_an_unknown_run_is_a_404(monkeypatch):
    reader = _DeletingReader([])
    _fake_connections(monkeypatch)
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.delete("/jobs/06baed8c-a6c9-47c1-ab0c-cc38a77f260c")
        assert resp.status_code == 404
        # A non-uuid must not reach Postgres as a DataError either.
        assert client.delete("/jobs/not-a-uuid").status_code == 404
    app.dependency_overrides.clear()


def test_clearing_history_reports_how_many_went(monkeypatch):
    reader = _DeletingReader([])
    _fake_connections(monkeypatch)
    app = _app_with_reader(reader, monkeypatch)

    with TestClient(app) as client:
        resp = client.delete("/jobs")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 4}
    assert reader.cleared == 1
