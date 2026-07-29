"""The API planner and Hatchet worker must target the same domain database."""

from __future__ import annotations

import pytest

from reimagina.orchestration.database_identity import assert_same_domain_database


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_):
        pass

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)

    def close(self):
        pass


def test_rejects_api_and_worker_database_mismatch(monkeypatch):
    monkeypatch.setattr(
        "reimagina.orchestration.database_identity.psycopg2.connect",
        lambda _dsn: _Connection(("worker_db", "jobs_runtime.job_run")),
    )

    with pytest.raises(RuntimeError, match="different databases"):
        assert_same_domain_database(_Connection(("api_db",)), "postgresql://worker")


def test_rejects_unmigrated_direct_database(monkeypatch):
    monkeypatch.setattr(
        "reimagina.orchestration.database_identity.psycopg2.connect",
        lambda _dsn: _Connection(("api_db", None)),
    )

    with pytest.raises(RuntimeError, match="migrated reimagina"):
        assert_same_domain_database(_Connection(("api_db",)), "postgresql://worker")
