"""Run telemetry transport — Postgres LISTEN/NOTIFY.

Only the transport lives here. Payload construction and SSE framing moved to
``reimagina.orchestration.frames``, which every ops-plane implementation shares;
this module is specific to the Postgres-queue ops-plane and goes away with it.

Tasks ``emit`` a status change on one fixed channel; the SSE endpoint LISTENs
and filters by run_id. A unit event carries ``indicator``; the run-level
finalise event has ``indicator=None`` and the overall status, which tells the
stream to close.
"""

from __future__ import annotations

from typing import Any

from reimagina.orchestration.frames import notify_payload

CHANNEL = "job_events"


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
