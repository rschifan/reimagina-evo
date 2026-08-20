"""A client typo in an id is a client error, and must never be a 500.

Every id below reaches a column declared ``uuid``. Sent as a bare ``str``, a
malformed one travelled all the way to Postgres, came back as
``invalid input syntax for type uuid`` — a ``psycopg2.DataError`` — and landed on
``errors.database_error_handler``, which maps every non-connection driver error
to 500 on the grounds that it is "a shipped-SQL bug, not a transient condition".
That reasoning is right; the premise was not. It also aborted the request's
transaction on the way out.

Two shapes of answer, because the two questions differ:

``422`` for a **query or body** parameter. The client supplied a filter that
cannot be parsed, so FastAPI rejects it at the boundary and no SQL runs.

``404`` for the ``/jobs/{run_id}`` **path**. The id names a resource rather than
filtering one, and an id no run can have simply names no run — the same answer a
well-formed but unknown id gets, and the same reasoning
``dbos_orchestrator._from_workflow`` already applies to a database DBOS has never
launched against. Answered in ``runstore.get_run``, which is typed to return
``None``, rather than patched at the route.

No Postgres here on purpose: the point is that these never reach it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn

BAD = "not-a-uuid"
WELL_FORMED = "00000000-0000-0000-0000-000000000000"
SLUG = "urban_form_block_compactness"

# Every route that takes an id the client can mistype. Each 500'd before.
QUERY_AND_BODY = [
    ("GET", f"/indicators?aoi_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/values?scenario_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/stats?scenario_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/stats?aoi_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/values.csv?scenario_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/values.geojson?scenario_id={BAD}", None),
    ("GET", f"/indicators/{SLUG}/values.parquet?scenario_id={BAD}", None),
    ("GET", f"/features?aoi_id={BAD}", None),
    ("GET", f"/aois/{BAD}/summaries", None),
    (
        "POST",
        "/metrics/within-polygon",
        {
            "aoi_id": BAD,
            "type_slugs": ["manzana"],
            "polygon": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]],
            },
        },
    ),
]


@pytest.fixture
def client(monkeypatch):
    """An app whose database would raise if a route ever reached it."""

    def _explode():
        raise AssertionError(
            "a route reached the database with an id it should have rejected"
        )

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([_explode])
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("method", "url", "body"),
    QUERY_AND_BODY,
    ids=[f"{m} {u.split('?')[0]}" for m, u, _ in QUERY_AND_BODY],
)
def test_a_malformed_id_in_a_filter_is_422(client, method, url, body):
    resp = client.request(method, url, json=body)
    assert resp.status_code == 422, (
        f"{method} {url} answered {resp.status_code}; a malformed id is a client "
        "error and must be rejected before any SQL runs"
    )


def _install(monkeypatch, orchestrator):
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: orchestrator,
    )


@pytest.mark.parametrize("run_id", [BAD, WELL_FORMED], ids=["malformed", "unknown"])
def test_an_unusable_run_id_is_404_not_500(monkeypatch, run_id):
    """Both readings of "no such run" get the same answer.

    A malformed id and a well-formed unknown one are indistinguishable to a
    client polling the documented loop, and they should be indistinguishable in
    the response too.
    """

    class _FakeOrchestrator:
        def get_run(self, conn, rid):
            return None

    _install(monkeypatch, _FakeOrchestrator())

    def _fake_conn():
        yield object()

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(app) as c:
        assert c.get(f"/jobs/{run_id}").status_code == 404
    app.dependency_overrides.clear()


def test_malformed_run_id_on_detail_is_404_not_500(monkeypatch):
    """A non-uuid must not reach Postgres.

    ``orchestration_run.id`` is uuid, so a bad id surfaces as ``DataError``,
    which ``errors.database_error_handler`` maps to 500 *and* aborts the
    request's transaction on the way out — a 500 for what is plainly a typo.
    """
    from apps.api.app import create_app
    from apps.api.deps import get_db_conn

    class _FakeOrchestrator:
        def get_run(self, conn, run_id):
            return None

    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: _FakeOrchestrator(),
    )

    class _Reader:
        def get_run_row(self, run_id):
            return None

        def list_units_detailed(self, run_id):
            return []

    monkeypatch.setattr(
        "apps.api.routers.jobs.get_backend",
        lambda conn: type("_B", (), {"ops_run_reader": lambda self: _Reader()})(),
    )

    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: iter([object()])
    with TestClient(app) as c:
        for bad in ("not-a-uuid", "42", "1;DROP TABLE x"):
            assert c.get(f"/jobs/{bad}/detail").status_code == 404, bad
    app.dependency_overrides.clear()
