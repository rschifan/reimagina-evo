"""`get_run` must answer, not explode, on a database DBOS has never launched.

DBOS owns its own schema and creates it at ``launch()``. Until then
``dbos.workflow_status`` does not exist — and asking whether a run exists is
still a legitimate question with an obvious answer: no. The port's return type
is ``RunStatus | None``, so a database error reaching the caller is a contract
break, not a diagnosis.

This is invisible in local development, where the schema was created by some
earlier run and never goes away. It surfaced in CI, on a database freshly
migrated by Alembic for every job.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator


class _Orig:
    """The psycopg3 error SQLAlchemy wraps, reduced to the part we match on."""

    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"wrapped driver error {sqlstate}")
        self.orig = _Orig(sqlstate)


def _orchestrator_raising(monkeypatch, exc: Exception) -> DBOSOrchestrator:
    from reimagina.orchestration import dbos_orchestrator

    class _Client:
        def list_workflows(self, **_):
            raise exc

    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: _Client())
    return DBOSOrchestrator(registry=(), execution_mode="client")


def test_a_missing_engine_schema_reads_as_no_such_run(monkeypatch):
    """42P01 is undefined_table: no schema, therefore no workflows, therefore None."""
    orch = _orchestrator_raising(monkeypatch, _Wrapped("42P01"))
    assert orch._from_workflow("some-run-id") is None


def test_any_other_database_error_still_propagates(monkeypatch):
    """The guard must not become a blanket swallow.

    A connection failure or a permissions error means we do not know whether the
    run exists, which is the opposite of knowing it does not.
    """
    orch = _orchestrator_raising(
        monkeypatch, _Wrapped("42501")
    )  # insufficient_privilege
    with pytest.raises(Exception, match="42501"):
        orch._from_workflow("some-run-id")


def test_an_error_with_no_driver_cause_propagates(monkeypatch):
    """Nothing to match on is not a reason to report the run as absent."""
    orch = _orchestrator_raising(monkeypatch, RuntimeError("engine unreachable"))
    with pytest.raises(RuntimeError, match="engine unreachable"):
        orch._from_workflow("some-run-id")
