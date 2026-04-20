"""Tests that exercise the real connection lifecycle, not the dep override.

Everything else in `test_api_routes.py` overrides `get_db_conn` with the
per-test fixture so the API reuses the test's transaction (fast, no
commit leakage). That's great for shape assertions but never exercises
the pool lifecycle, the lifespan hook, or the `psycopg2.Error` handler.

These tests do. Each test spins up an isolated FastAPI app with a real
pool pointed at the session-level `migrated_db` DSN (which is guarded
against destructive teardown by conftest's name-safety rail), hits the
API with a real httpx client, and asserts the outcome. On teardown the
lifespan shuts the pool down — zero leaked connections.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.api import deps
from apps.api.app import create_app


@pytest.fixture
def real_conn_client(migrated_db, monkeypatch) -> Iterator[TestClient]:
    """TestClient with a live pool against the migrated test DB.

    `migrated_db` is the session-scoped fixture in tests/conftest.py that
    Alembic-migrates whatever DB TEST_DATABASE_URL points at (with a
    name-safety rail — refuses non-`*test*` names).  Setting
    REIMAGINA_APP_DSN steers `deps.api_dsn()` at the pool factory + the
    readiness probe without patching module state.
    """
    deps.shutdown_pool()
    monkeypatch.setenv("REIMAGINA_APP_DSN", migrated_db)
    app = create_app(initialise_pool=True)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        # Belt-and-braces: ensure the pool is closed even if lifespan
        # didn't fire (e.g. if TestClient raised during setup).
        deps.shutdown_pool()


def test_pool_backed_liveness(real_conn_client):
    response = real_conn_client.get("/health/live")
    assert response.status_code == 200


def test_pool_backed_readiness(real_conn_client):
    response = real_conn_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["db"] == "reachable"


def test_pool_backed_catalog_list_returns_list(real_conn_client, migrated_db):
    """Catalog endpoint round-trips through the real pool.

    We assert structural shape only, not content: session-scoped
    `migrated_db` may leave committed catalog rows from earlier pytest
    sessions (the fixture upgrades but doesn't down-then-up), and this
    test isn't the right place to police content — dep-overridden
    tests in test_api_routes.py already do that.
    """
    response = real_conn_client.get("/indicators")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_pool_survives_consecutive_requests(real_conn_client):
    """Ten calls back-to-back must all succeed — no pool exhaustion."""
    for _ in range(10):
        response = real_conn_client.get("/health/ready")
        assert response.status_code == 200


def test_psycopg_operational_error_produces_structured_503(monkeypatch, real_conn_client):
    """Simulated connectivity outage → 503 + database_unavailable."""
    import psycopg2

    from apps.api.deps import get_db_conn
    def _broken_conn():
        raise psycopg2.OperationalError("simulated outage")
        yield  # pragma: no cover — makes this a generator fn for the dep

    real_conn_client.app.dependency_overrides[get_db_conn] = _broken_conn
    try:
        response = real_conn_client.get("/indicators")
    finally:
        real_conn_client.app.dependency_overrides.pop(get_db_conn, None)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    assert "request_id" in body["error"]
    assert response.headers["X-Request-Id"] == body["error"]["request_id"]


def test_psycopg_programming_error_produces_500(monkeypatch, real_conn_client):
    """A shipped-SQL bug must NOT masquerade as a transient 503.

    ProgrammingError is a programming fault, not a connectivity problem —
    retrying will never help. Keeping it on the 500 path means clients
    and operators get the right signal.
    """
    import psycopg2

    from apps.api.deps import get_db_conn
    def _buggy_conn():
        raise psycopg2.ProgrammingError("syntax error at or near ...")
        yield  # pragma: no cover

    real_conn_client.app.dependency_overrides[get_db_conn] = _buggy_conn
    try:
        response = real_conn_client.get("/indicators")
    finally:
        real_conn_client.app.dependency_overrides.pop(get_db_conn, None)

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "database_error"
