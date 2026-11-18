"""`ensure_system_database` refuses a pooler before DBOS migrates through it.

The proof that the probe tells pgbouncer from Postgres is the integration test
beside `test_ensure_system_database_refuses_a_database_that_is_not_ours`, which
needs a real pooler and so does not run in CI. This is the part CI can hold: the
refusal branch is reached, and nothing is created once it is.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import dbos_app


class _PooledConnection:
    """A client connection to a transaction-mode pooler with one idle backend.

    Every client is handed the same server backend between statements, which
    is what pgbouncer measurably does with an idle pool: two connections open
    side by side both report one `pg_backend_pid()`.
    """

    autocommit = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        pass

    def fetchone(self):
        # The backend's pid for the probe; for the domain-table check, any
        # non-NULL answer reads as "jobs_runtime.job_run exists".
        return (4242,)

    def close(self):
        pass


class _SystemDatabase:
    def run_migrations(self):
        pass

    def destroy(self):
        pass


def test_a_pooled_dsn_is_refused_before_anything_is_migrated(monkeypatch):
    created: list[str] = []

    def _create(**kwargs):
        created.append(kwargs["system_database_url"])
        return _SystemDatabase()

    monkeypatch.setattr(dbos_app.psycopg2, "connect", lambda dsn: _PooledConnection())
    monkeypatch.setattr("dbos._sys_db.SystemDatabase.create", _create)

    with pytest.raises(RuntimeError, match="DATABASE_URL_DIRECT"):
        dbos_app.ensure_system_database("postgresql://u:secret@pooler:6432/reimagina")

    assert created == [], "the refusal must come before DBOS touches the database"
