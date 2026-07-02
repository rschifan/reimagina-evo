"""Run telemetry (Phase 3, component 12b) — Postgres LISTEN/NOTIFY + SSE frames.

Tasks ``emit`` a status change on one fixed channel; the SSE endpoint LISTENs
and filters by run_id. A unit event carries ``indicator``; the run-level
finalise event has ``indicator=None`` and the overall status, which tells the
stream to close.
"""

from __future__ import annotations

import json
from typing import Any

CHANNEL = "job_events"
_RUN_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})


def notify_payload(*, run_id: str, indicator: str | None, status: str) -> str:
    return json.dumps({"run_id": run_id, "indicator": indicator, "status": status})


def emit(conn: Any, *, run_id: str, indicator: str | None, status: str) -> None:
    """NOTIFY a status change (Postgres only; no-op elsewhere).

    Delivered to listeners when the caller's transaction commits, so call this
    before the commit that persists the same status change.
    """
    from reimagina.storage import backend_for_connection

    try:
        if backend_for_connection(conn).name != "postgres":
            return
    except TypeError:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_notify(%s, %s)",
            (
                CHANNEL,
                notify_payload(run_id=run_id, indicator=indicator, status=status),
            ),
        )


def sse_frame(payload: str) -> str:
    """Format a JSON payload as one Server-Sent Events data frame."""
    return f"data: {payload}\n\n"


def is_run_terminal(status: str) -> bool:
    return status in _RUN_TERMINAL
