"""Intake API (Phase 3, component 2) — submit a scenario evaluation, read status.

``POST /jobs`` enqueues a run on the Procrastinate queue and returns ``202``
immediately (no compute in the request path — a worker drains it). The enqueue
runs on a dedicated committed connection, NOT the API's rollback-scoped pool.
``GET /jobs/{run_id}`` reads the persisted run + per-unit status.

Requires ``DATABASE_URL_DIRECT`` + a running ``python -m apps.worker``.
"""

from __future__ import annotations

import json
import os
import select
import time
from collections.abc import Iterator

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..deps import api_dsn, get_db_conn

_STREAM_MAX_SECONDS = 300.0
_HEARTBEAT_SECONDS = 15.0

router = APIRouter(prefix="/jobs", tags=["jobs"])


class SubmitRunBody(BaseModel):
    changed_attributes: list[str]
    edited_features: list[str]
    scenario_id: str | None = None
    aoi_id: str | None = None


class SubmitRunResponse(BaseModel):
    run_id: str
    status: str


class UnitView(BaseModel):
    indicator: str
    status: str
    detail: str | None = None


class RunView(BaseModel):
    run_id: str
    status: str
    units: list[UnitView] = []


@router.post("", status_code=202, response_model=SubmitRunResponse)
def submit_run(body: SubmitRunBody) -> SubmitRunResponse:
    from reimagina.db import connect
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )
    from reimagina.provenance import code_version

    # Enqueue on a dedicated committed connection — the API pool connection
    # rolls back on teardown, which would discard the run + queued units.
    with connect(api_dsn()) as conn:
        handle = ProcrastinateOrchestrator().submit_run(
            conn,
            changed_attributes=body.changed_attributes,
            edited_features=body.edited_features,
            scenario_id=body.scenario_id,
            aoi_id=body.aoi_id,
            code_version=code_version(),
        )
    return SubmitRunResponse(run_id=handle.run_id, status="queued")


@router.get("/{run_id}", response_model=RunView)
def get_run(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunView:
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )

    status = ProcrastinateOrchestrator().get_run(conn, run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return RunView(
        run_id=status.run_id,
        status=status.status,
        units=[
            UnitView(indicator=u.indicator, status=u.status, detail=u.detail)
            for u in status.units
        ],
    )


def _event_stream(run_id: str) -> Iterator[str]:
    """SSE generator: snapshot frame, then LISTEN/NOTIFY updates until terminal.

    Own direct connection (LISTEN needs a non-transaction-pooled connection),
    filtered to this run, capped lifetime with heartbeats.
    """
    from reimagina.orchestration import events, runstore

    dsn = os.environ.get("DATABASE_URL_DIRECT") or api_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"LISTEN {events.CHANNEL}")
        # Read the snapshot AFTER LISTEN so a finalise between the two isn't lost.
        current = runstore.get_run(conn, run_id)
        status = current["status"] if current else "unknown"
        yield events.sse_frame(
            events.notify_payload(run_id=run_id, indicator=None, status=status)
        )
        if events.is_run_terminal(status):
            return
        deadline = time.monotonic() + _STREAM_MAX_SECONDS
        while time.monotonic() < deadline:
            if select.select([conn], [], [], _HEARTBEAT_SECONDS)[0]:
                conn.poll()
                while conn.notifies:
                    note = conn.notifies.pop(0)
                    data = json.loads(note.payload)
                    if data.get("run_id") != run_id:
                        continue
                    yield events.sse_frame(note.payload)
                    if data.get("indicator") is None and events.is_run_terminal(
                        data.get("status", "")
                    ):
                        return
            else:
                yield ": keep-alive\n\n"
    finally:
        conn.close()


@router.get("/{run_id}/events")
def stream_run_events(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> StreamingResponse:
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )

    if ProcrastinateOrchestrator().get_run(conn, run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return StreamingResponse(_event_stream(run_id), media_type="text/event-stream")
