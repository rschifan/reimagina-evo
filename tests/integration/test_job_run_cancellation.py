"""The cancellation seam (r031): run correlation, backend pid, pg_cancel_backend.

Cancelling a run only means something if it can reach the Postgres backend
actually executing the work — no orchestration engine can interrupt a thread
blocked in a psycopg C call. These tests cover the three pieces that make that
possible and are otherwise easy to break silently: the run id being recorded,
the pid being the *working* connection's, and the cancel actually landing on a
live statement.
"""

from __future__ import annotations

import threading
import time

import psycopg2
import pytest

from reimagina.jobs._lifecycle import current_run, job_run
from reimagina.storage import backend_for_connection


def _row(conn, job_run_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id::text, backend_pid, status "
            "FROM jobs_runtime.job_run WHERE id = %s",
            (job_run_id,),
        )
        run_id, backend_pid, status = cur.fetchone()
    return {"run_id": run_id, "backend_pid": backend_pid, "status": status}


def _backend_pid(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return int(cur.fetchone()[0])


def test_job_run_records_the_ambient_run_and_its_own_backend_pid(conn):
    backend = backend_for_connection(conn)

    with current_run("11111111-1111-1111-1111-111111111111"):
        with job_run(
            backend, model_family="urban_form", code_version="test", scenario_id=None
        ) as job_run_id:
            pass

    row = _row(conn, job_run_id)
    assert row["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert row["status"] == "succeeded"
    # The pid must be *this* connection's — the one that would run the compute.
    # Capturing it anywhere else records a backend nobody needs to cancel.
    assert row["backend_pid"] == _backend_pid(conn)


def test_a_job_outside_any_run_has_a_null_run_id(conn):
    """A direct `indicators compute` belongs to no run. That is not an error."""
    backend = backend_for_connection(conn)

    with job_run(
        backend, model_family="urban_form", code_version="test", scenario_id=None
    ) as job_run_id:
        pass

    assert _row(conn, job_run_id)["run_id"] is None


def test_the_ambient_run_does_not_leak_past_its_scope(conn):
    """A stale run id would mis-attribute the next job on this thread."""
    backend = backend_for_connection(conn)

    with current_run("22222222-2222-2222-2222-222222222222"):
        with job_run(
            backend, model_family="urban_form", code_version="test", scenario_id=None
        ) as inside:
            pass
    with job_run(
        backend, model_family="urban_form", code_version="test", scenario_id=None
    ) as outside:
        pass

    assert _row(conn, inside)["run_id"] is not None
    assert _row(conn, outside)["run_id"] is None


def test_a_failed_job_still_records_the_run(conn):
    """Cancellation looks for live rows; a failure must not lose its correlation."""
    backend = backend_for_connection(conn)
    run = "33333333-3333-3333-3333-333333333333"

    with pytest.raises(RuntimeError):
        with current_run(run):
            with job_run(
                backend,
                model_family="urban_form",
                code_version="test",
                scenario_id=None,
            ) as job_run_id:
                raise RuntimeError("boom")

    row = _row(conn, job_run_id)
    assert row["run_id"] == run
    assert row["status"] == "failed"


def test_cancel_run_backends_stops_a_live_statement(migrated_db):
    """The mechanism 0.7 exists for: an in-flight query actually stops.

    Uses a real second connection running a long statement, because that is the
    only thing that distinguishes this from bookkeeping — marking a unit
    ``cancelled`` while its PostGIS query runs to completion is precisely the
    failure mode being closed.
    """
    run = "44444444-4444-4444-4444-444444444444"
    worker = psycopg2.connect(migrated_db)
    worker.autocommit = True
    control = psycopg2.connect(migrated_db)
    control.autocommit = True
    error: list[BaseException] = []
    started = threading.Event()

    def _long_query():
        try:
            with worker.cursor() as cur:
                started.set()
                cur.execute("SELECT pg_sleep(30)")
        except BaseException as exc:  # noqa: BLE001 — the cancel is the assertion
            error.append(exc)

    try:
        with control.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs_runtime.job_run "
                "(run_id, model_family, code_version, status, backend_pid) "
                "VALUES (%s, 'urban_form', 'test', 'running', %s)",
                (run, _backend_pid(worker)),
            )

        thread = threading.Thread(target=_long_query, daemon=True)
        thread.start()
        assert started.wait(5.0)
        time.sleep(0.3)  # let the statement reach the server

        signalled = (
            backend_for_connection(control).job_run_repo().cancel_run_backends(run)
        )
        assert signalled == 1

        thread.join(timeout=10.0)
        assert not thread.is_alive(), "pg_sleep survived the cancel"
        assert error and isinstance(error[0], psycopg2.errors.QueryCanceled)
    finally:
        with control.cursor() as cur:
            cur.execute("DELETE FROM jobs_runtime.job_run WHERE run_id = %s", (run,))
        worker.close()
        control.close()

def test_cancel_skips_terminal_rows(migrated_db):
    """A finished job's pid may have been reused by an unrelated backend."""
    run = "55555555-5555-5555-5555-555555555555"
    control = psycopg2.connect(migrated_db)
    control.autocommit = True
    try:
        with control.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs_runtime.job_run "
                "(run_id, model_family, code_version, status, backend_pid) "
                "VALUES (%s, 'urban_form', 'test', 'succeeded', %s)",
                (run, _backend_pid(control)),
            )
        assert (
            backend_for_connection(control).job_run_repo().cancel_run_backends(run) == 0
        )
    finally:
        with control.cursor() as cur:
            cur.execute("DELETE FROM jobs_runtime.job_run WHERE run_id = %s", (run,))
        control.close()
