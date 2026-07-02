"""Durable orchestration run/unit state (jobs_runtime.orchestration_run + job_unit).

Postgres-only ops-plane state (component 5). DuckDB is frozen at r024 and has no
such tables, so every function no-ops there (and for a ``None`` connection in
unit tests) — the synchronous orchestrator then returns its in-memory RunReport
view instead of a persisted one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

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
    """Insert the run envelope. Returns True if persisted, False when skipped."""
    if not _is_postgres(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.orchestration_run "
            "(id, scenario_id, aoi_id, status, code_version) VALUES (%s, %s, %s, %s, %s)",
            (run_id, scenario_id, aoi_id, status, code_version),
        )
    return True


def run_params(conn: Any, run_id: str) -> dict[str, Any] | None:
    """Return the run-level params the async tasks need to dispatch units."""
    if not _is_postgres(conn):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scenario_id::text, aoi_id, code_version "
            "FROM jobs_runtime.orchestration_run WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"scenario_id": row[0], "aoi_id": row[1], "code_version": row[2]}


def set_unit_status(
    conn: Any,
    *,
    run_id: str,
    indicator_slug: str,
    status: str,
    detail: str | None = None,
) -> None:
    if not _is_postgres(conn):
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.job_unit SET status = %s, detail = %s, "
            "started_at = COALESCE(started_at, CASE WHEN %s = 'running' THEN now() END), "
            "completed_at = CASE WHEN %s IN ('succeeded','failed','skipped','cancelled') "
            "THEN now() ELSE completed_at END "
            "WHERE run_id = %s AND indicator_slug = %s",
            (status, detail, status, status, run_id, indicator_slug),
        )


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
    ``{indicator, features(list[str]), status, detail}``."""
    if not _is_postgres(conn):
        return
    with conn.cursor() as cur:
        for unit in units:
            cur.execute(
                "INSERT INTO jobs_runtime.job_unit "
                "(run_id, indicator_slug, feature_scope, status, detail) "
                "VALUES (%s, %s, %s::jsonb, %s, %s)",
                (
                    run_id,
                    unit["indicator"],
                    json.dumps(sorted(unit["features"])),
                    unit["status"],
                    unit.get("detail"),
                ),
            )


def finalize_run(conn: Any, *, run_id: str, status: str) -> None:
    if not _is_postgres(conn):
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.orchestration_run "
            "SET status = %s, completed_at = now() WHERE id = %s",
            (status, run_id),
        )


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
