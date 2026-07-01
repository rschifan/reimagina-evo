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
    status: str = "running",
) -> bool:
    """Insert the run envelope. Returns True if persisted, False when skipped."""
    if not _is_postgres(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.orchestration_run "
            "(id, scenario_id, status, code_version) VALUES (%s, %s, %s, %s)",
            (run_id, scenario_id, status, code_version),
        )
    return True


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
