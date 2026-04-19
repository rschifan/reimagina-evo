"""Shared pytest fixtures for all test suites.

Requirements:
  - A running PostgreSQL instance with PostGIS installed.
  - TEST_DATABASE_URL env var (or postgresql://localhost/reimagina_test by default).

The session-scoped `migrated_db` fixture runs all Alembic migrations once per
test session and tears them down at the end. The `conn` fixture wraps each test
in a transaction that is rolled back on completion, giving test isolation without
re-running migrations for every test.

SAFETY: the teardown step (downgrade-to-base) is destructive — it drops every
table the migrations manage. We therefore refuse to run against a DB whose
name does not clearly look like a test DB (contains `test`). If someone
points TEST_DATABASE_URL at a dev or prod DB, the fixture raises at setup
instead of silently wiping the data at teardown.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest
from alembic.config import Config
from alembic import command

PY_PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages" / "py"
if str(PY_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PY_PACKAGES_DIR))


def _test_db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/reimagina_test")


def _assert_db_name_is_test_safe(db_url: str) -> None:
    """Refuse to run the destructive migration fixture against a non-test DB.

    The teardown path calls `alembic downgrade base`, which drops every
    tracked schema. Accidentally pointing this at a dev/prod DB via
    TEST_DATABASE_URL would wipe real data, so we hard-fail unless the DB
    name makes it obvious this is a test target.
    """
    db_name = urlparse(db_url).path.lstrip("/")
    allowed = os.environ.get("REIMAGINA_TEST_DB_ALLOW_NAME", "")
    if "test" in db_name.lower() or db_name == allowed:
        return
    raise RuntimeError(
        "Refusing to run the migrated_db fixture against "
        f"{db_name!r}: the database name must contain 'test' so the "
        "teardown (downgrade-to-base) can't wipe real data. If this is "
        "intentional, set REIMAGINA_TEST_DB_ALLOW_NAME to the exact DB "
        "name to opt in."
    )


@pytest.fixture(scope="session")
def db_url() -> str:
    return _test_db_url()


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:  # type: ignore[return]
    """Run all migrations against the test DB once per session, then tear down."""
    _assert_db_name_is_test_safe(db_url)
    try:
        probe = psycopg2.connect(db_url)
        probe.close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL test database unavailable: {exc}")

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    yield db_url
    command.downgrade(cfg, "base")


@pytest.fixture
def conn(migrated_db: str):
    """Provide a psycopg2 connection that rolls back after each test."""
    connection = psycopg2.connect(migrated_db)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture(scope="session")
def perf_db_url() -> str:
    """DSN for benchmarks that expect pre-loaded real data (e.g. INEGI CDMX).

    Defaults to REIMAGINA_PERF_DSN → REIMAGINA_APP_DSN → postgresql://localhost/reimagina_app.
    This fixture intentionally does NOT run migrations and does NOT tear down,
    so pointing it at a dev DB with real data is safe by construction.
    """
    return (
        os.environ.get("REIMAGINA_PERF_DSN")
        or os.environ.get("REIMAGINA_APP_DSN")
        or "postgresql://localhost/reimagina_app"
    )


@pytest.fixture
def perf_conn(perf_db_url: str):
    """Read-oriented psycopg2 connection for perf benchmarks.

    Does not manage migrations or schema. The caller is expected to have
    loaded realistic data (e.g. via `reimagina-cli cdmx bootstrap`) — tests
    should skip when the expected data is absent rather than attempt setup.
    The transaction rolls back on teardown so incidental temp-table creation
    or lock acquisition inside a benchmark can't leak.
    """
    try:
        connection = psycopg2.connect(perf_db_url)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"Perf DB unavailable at {perf_db_url!r}: {exc}")
        return  # type: ignore[return-value]
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
