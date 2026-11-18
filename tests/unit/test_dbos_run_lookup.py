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


def _queued_row(**overrides):
    from types import SimpleNamespace

    row = dict(
        workflow_id="run-1",
        status="ENQUEUED",
        created_at=1_755_684_000_000,  # 2025-08-20T10:00:00Z, epoch ms
        input={
            "args": (
                "run-1",
                [{"slug": "urban_form_block_compactness"}],
                {"scenario_id": "scn-1", "aoi_id": "aoi-1", "plan_id": "run-1"},
            )
        },
    )
    row.update(overrides)
    return SimpleNamespace(**row)


def test_a_pre_envelope_run_carries_the_engines_creation_time():
    """Without it the detail page re-stamped a queued run as "now" every poll."""
    from datetime import datetime, timezone

    from reimagina.orchestration.dbos_orchestrator import _as_run_status

    status = _as_run_status(_queued_row())
    assert status.submitted_at == datetime(2025, 8, 20, 10, 0, tzinfo=timezone.utc)
    assert _as_run_status(_queued_row(created_at=None)).submitted_at is None


def test_cancelling_before_the_envelope_keeps_scope_and_units(monkeypatch):
    """The fallback envelope is the only one the run will ever get.

    It used to be written with no scenario/AOI and no `job_unit` rows, so the
    cancelled run showed zero units and `/rerun` answered "every indicator
    succeeded" for a run in which nothing ran.
    """
    from types import SimpleNamespace

    from reimagina.orchestration import dbos_orchestrator, runstore

    written: dict = {}

    class _Client:
        def list_workflows(self, **_):
            return [_queued_row()]

        def cancel_workflow(self, *_, **__):
            pass

    monkeypatch.setattr(dbos_orchestrator, "_client", lambda: _Client())
    monkeypatch.setattr(
        dbos_orchestrator,
        "backend_for_connection",
        lambda conn: SimpleNamespace(
            job_run_repo=lambda: SimpleNamespace(cancel_run_backends=lambda rid: 0)
        ),
    )
    monkeypatch.setattr(runstore, "get_run", lambda conn, rid: None)
    monkeypatch.setattr(runstore, "set_unit_status", lambda conn, **kw: False)
    monkeypatch.setattr(runstore, "list_units", lambda conn, rid: [])
    monkeypatch.setattr(runstore, "finalize_run", lambda conn, **kw: False)
    monkeypatch.setattr(
        runstore, "create_run", lambda conn, **kw: written.update(run=kw)
    )
    monkeypatch.setattr(
        runstore, "record_units", lambda conn, **kw: written.update(units=kw["units"])
    )

    orch = DBOSOrchestrator(registry=(), execution_mode="client")
    assert orch.cancel_run(SimpleNamespace(commit=lambda: None), "run-1") is True

    assert written["run"]["scenario_id"] == "scn-1"
    assert written["run"]["aoi_id"] == "aoi-1"
    assert written["run"]["status"] == "cancelled"
    assert [(u["indicator"], u["status"]) for u in written["units"]] == [
        ("urban_form_block_compactness", "cancelled")
    ]
