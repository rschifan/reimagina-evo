"""Durable orchestration run/unit state (jobs_runtime.orchestration_run + job_unit).

Postgres-only ops-plane state (component 5): the run envelope and one row per
unit. This is *our* record of a run, which is exactly what an engine that owns
run state natively replaces — so this module, and both tables, are among the
things an ops-plane branch may delete outright.

Every function no-ops for a ``None`` connection, which keeps it injectable
alongside ``memstore.InMemoryRunStore``; the synchronous orchestrator takes the
in-memory one by default and never touches these tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from reimagina.orchestration.port import RUN_TERMINAL_STATES, UNIT_TERMINAL_STATES
from reimagina.storage import backend_for_connection


def _is_postgres(conn: Any) -> bool:
    try:
        return backend_for_connection(conn).name == "postgres"
    except TypeError:
        return False


def create_run(
    conn: Any,
    *,
    run_id: str,
    scenario_id: str | None,
    code_version: str,
    aoi_id: str | None = None,
    status: str = "running",
) -> bool:
    """Insert the run envelope. Returns True if persisted, False when skipped.

    Idempotent, because the ops-plane may re-run the step that calls it. A
    durable-execution step only records its output once it completes, so one
    interrupted partway through starts again from the top on recovery — and a
    bare INSERT would then fail on the primary key, permanently. A run that
    could not survive a crash during its own first step would defeat the point
    of durable execution entirely.

    ``DO NOTHING`` rather than ``DO UPDATE``: the row may have been advanced
    since (to a terminal status, by a cancel), and re-opening a run that has
    already closed is worse than not writing at all.
    """
    if not _is_postgres(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.orchestration_run "
            "(id, scenario_id, aoi_id, status, code_version) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (run_id, scenario_id, aoi_id, status, code_version),
        )
    return True


def set_unit_status(
    conn: Any,
    *,
    run_id: str,
    indicator_slug: str,
    status: str,
    detail: str | None = None,
) -> bool:
    """Move a unit's status unless it is already terminal; True if it moved.

    The terminal guard is what makes cancellation stick. Cancelling marks a
    running unit ``cancelled``, but the worker executing it does not stop — its
    query is cancelled, it catches the resulting error, and it writes ``failed``
    over the top, resurrecting a run that was already closed. A unit that has
    reached a terminal state has reached its final one.

    The return value is not decoration: a caller that announces the transition
    it just asked for, without checking whether it happened, publishes a status
    the database refused. That is how an SSE stream ends up reporting ``failed``
    for a unit ``GET /jobs/{run_id}`` calls ``cancelled``.
    """
    if not _is_postgres(conn):
        return False
    terminal = tuple(sorted(UNIT_TERMINAL_STATES))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.job_unit SET status = %s, detail = %s, "
            "started_at = COALESCE(started_at, CASE WHEN %s = 'running' THEN now() END), "
            "completed_at = CASE WHEN %s IN %s THEN now() ELSE completed_at END "
            "WHERE run_id = %s AND indicator_slug = %s "
            "AND status NOT IN %s",
            (
                status,
                detail,
                status,
                status,
                terminal,
                run_id,
                indicator_slug,
                terminal,
            ),
        )
        return cur.rowcount > 0


def list_units(conn: Any, run_id: str) -> list[dict[str, Any]]:
    """Return ``[{indicator, status, features(list[str]), detail}]`` for a run."""
    if not _is_postgres(conn):
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indicator_slug, status, feature_scope, detail "
            "FROM jobs_runtime.job_unit WHERE run_id = %s ORDER BY indicator_slug",
            (run_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "indicator": r[0],
            "status": r[1],
            "features": list(r[2] or []),
            "detail": r[3],
        }
        for r in rows
    ]


def record_units(conn: Any, *, run_id: str, units: Sequence[dict[str, Any]]) -> None:
    """Insert one job_unit per dirty unit. ``units`` items:
    ``{indicator, features(list[str]), status, detail}``.

    Idempotent for the same reason as ``create_run`` — and here ``DO NOTHING``
    does more than avoid an error. Units that already progressed keep the status
    they reached: a re-run must not reset a succeeded unit to ``queued`` and
    schedule its minutes of PostGIS a second time.
    """
    if not _is_postgres(conn):
        return
    with conn.cursor() as cur:
        for unit in units:
            cur.execute(
                "INSERT INTO jobs_runtime.job_unit "
                "(run_id, indicator_slug, feature_scope, status, detail) "
                "VALUES (%s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (run_id, indicator_slug) DO NOTHING",
                (
                    run_id,
                    unit["indicator"],
                    json.dumps(sorted(unit["features"])),
                    unit["status"],
                    unit.get("detail"),
                ),
            )


def finalize_run(conn: Any, *, run_id: str, status: str) -> bool:
    """Close a run unless it is already closed; True if this call closed it.

    Same guard as ``set_unit_status``, and needed for the same reason. Cancelling
    closes the run, then the worker that was mid-unit finishes, advances, and
    finalises again — re-stamping ``completed_at`` with a later ``now()`` and
    publishing a second run-level terminal event. The first close is the real
    one: it is when the run stopped being able to change.
    """
    if not _is_postgres(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.orchestration_run "
            "SET status = %s, completed_at = now() "
            "WHERE id = %s AND status NOT IN %s",
            (status, run_id, tuple(sorted(RUN_TERMINAL_STATES))),
        )
        return cur.rowcount > 0


def get_run(conn: Any, run_id: str) -> dict[str, Any] | None:
    """Return ``{run_id, status, units:[{indicator,status,detail}]}`` or None."""
    if not _is_postgres(conn):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, status FROM jobs_runtime.orchestration_run WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "SELECT indicator_slug, status, detail FROM jobs_runtime.job_unit "
            "WHERE run_id = %s ORDER BY indicator_slug",
            (run_id,),
        )
        units = [
            {"indicator": r[0], "status": r[1], "detail": r[2]} for r in cur.fetchall()
        ]
    return {"run_id": row[0], "status": row[1], "units": units}
