"""Shared pytest fixtures for all test suites.

Requirements:
  - A running PostgreSQL instance with PostGIS installed.
  - TEST_DATABASE_URL env var (or postgresql://localhost/reimagina_test by default).

The session-scoped `migrated_db` fixture runs all Alembic migrations once per
test session and tears them down at the end. The `conn` fixture wraps each test
in a transaction that is rolled back on completion, giving test isolation without
re-running migrations for every test.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
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


@pytest.fixture(scope="session")
def db_url() -> str:
    return _test_db_url()


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:  # type: ignore[return]
    """Run all migrations against the test DB once per session, then tear down."""
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
