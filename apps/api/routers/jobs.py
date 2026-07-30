"""Intake API (Phase 3, component 2) — submit a scenario evaluation, read status.

``POST /jobs`` enqueues a run and returns ``202`` immediately. Hatchet workers
execute the graph outside the request path; callers use the status and SSE
endpoints rather than holding an API worker thread until PostGIS completes. The
enqueue uses a dedicated committed connection, not the API's rollback-scoped
pool. ``GET /jobs/{run_id}`` reads the engine-backed run and per-unit status.

Every path goes through ``orchestration.factory.get_orchestrator`` — this module
names no engine, so swapping the ops-plane does not touch it.

Requires ``DATABASE_URL_DIRECT`` + a running ``python -m apps.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..deps import api_dsn, get_db_conn

_STREAM_MAX_SECONDS = 300.0
_HEARTBEAT_SECONDS = 15.0

router = APIRouter(prefix="/jobs", tags=["jobs"])


class SubmitRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Optional: when a scenario_id is given and these are omitted, the change-set
    # is derived from the scenario's overrides (the override rows are the diff).
    changed_attributes: list[str] | None = None
    edited_features: list[str] | None = None
    scenario_id: str | None = None
    aoi_id: str | None = None

    wait: Literal[False] = False
    """Accepted only as ``false``; ``true`` is a 422.

    ⚠ Declared rather than left to ``extra="forbid"``. ``wait`` used to start a
    worker inside the request, which is gone — but it defaulted to ``false``, so
    forbidding the key outright also rejected clients that merely spelled out
    that default and asked for the only behaviour this endpoint still has.
    Rejecting ``true`` is the contract change; rejecting ``false`` was
    collateral.
    """


class SubmitRunResponse(BaseModel):
    # None for a "noop" submit: the dirty-set was empty, so no run row exists
    # and GET /jobs/{run_id} would 404. Handing back an unusable id makes a
    # client following the documented poll loop fail after a *successful* submit.
    run_id: str | None
    status: str


class UnitView(BaseModel):
    indicator: str
    status: str
    detail: str | None = None


class RunView(BaseModel):
    run_id: str
    status: str
    units: list[UnitView] = []


def _submit_response(run_id: str, status: str) -> SubmitRunResponse:
    """Suppress the run_id on a noop — nothing was persisted to poll for."""
    return SubmitRunResponse(run_id=None if status == "noop" else run_id, status=status)


@router.post("", status_code=202, response_model=SubmitRunResponse)
def submit_run(body: SubmitRunBody) -> SubmitRunResponse:
    from reimagina.db import connect
    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.provenance import code_version

    orchestrator = get_orchestrator()

    # Enqueue on a dedicated committed connection — the API pool connection
    # rolls back on teardown, which would discard the run + queued units.
    with contextlib.closing(connect(api_dsn())) as conn:
        handle = orchestrator.submit_run(
            conn,
            changed_attributes=body.changed_attributes or [],
            edited_features=body.edited_features or [],
            scenario_id=body.scenario_id,
            aoi_id=body.aoi_id,
            code_version=code_version(),
        )

    # "noop" when the run had nothing to recompute; else the queued run_id.
    status = handle.status.status if handle.status else "queued"
    return _submit_response(handle.run_id, status)


@router.get("/{run_id}", response_model=RunView)
def get_run(
    run_id: UUID,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunView:
    """⚠ ``run_id: UUID``, not ``str``. A run id *is* a uuid, and typing it as
    one rejects a malformed id here, with 422, instead of letting it reach the
    engine client — whose own length validation raises a pydantic error carrying
    no HTTP status, which the orchestrator then cannot tell from an outage and
    reports as a retryable 503. Validating at the boundary deletes that whole
    special case."""
    from reimagina.orchestration.factory import get_orchestrator

    status = get_orchestrator().get_run(conn, str(run_id))
    if status is None:
        raise HTTPException(status_code=404, detail=f"run {str(run_id)!r} not found")
    return RunView(
        run_id=status.run_id,
        status=status.status,
        units=[
            UnitView(indicator=u.indicator, status=u.status, detail=u.detail)
            for u in status.units
        ],
    )


async def _event_stream(run_id: str) -> AsyncIterator[str]:
    """Frame the orchestrator's progress events as SSE, with route-owned policy.

    The orchestrator decides *what* happened; this decides how it reaches the
    wire — frame format, keep-alive cadence, and the lifetime cap. Where the
    events come from (LISTEN/NOTIFY, a workflow stream, a websocket) is not this
    module's business.
    """
    from reimagina.orchestration import frames
    from reimagina.orchestration.factory import get_orchestrator

    events = get_orchestrator().subscribe_progress(run_id).__aiter__()
    deadline = time.monotonic() + _STREAM_MAX_SECONDS
    # Hold one pending __anext__ across heartbeats rather than re-awaiting it.
    # Wrapping the await in a timeout would cancel it mid-flight, which leaves
    # an async generator unresumable — the keep-alive would break the stream it
    # exists to keep open.
    pending = asyncio.ensure_future(anext(events))
    try:
        while time.monotonic() < deadline:
            done, _ = await asyncio.wait({pending}, timeout=_HEARTBEAT_SECONDS)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            except Exception:  # noqa: BLE001 — see below; silence is worse
                # ⚠ The response is already 200 with its headers on the wire, so
                # the registered 503 handler cannot run: Starlette raises
                # "response already started" rather than replacing it. Letting
                # the exception escape therefore just stops the stream, which a
                # client cannot distinguish from a completed run.
                #
                # Broad on purpose, and engine-agnostic on purpose: whatever
                # went wrong reading progress, the caller needs to be told the
                # stream ended badly, and this module names no engine. Normal
                # termination is `StopAsyncIteration`, caught above; a client
                # disconnect is `CancelledError`, a BaseException, and still
                # propagates.
                #
                # A fixed reason string, never the exception text — the
                # orchestrator's message carries the run id and its `__cause__`
                # carries engine internals. Neither belongs on a public stream.
                yield frames.error_frame("orchestrator_unavailable")
                return
            pending = asyncio.ensure_future(anext(events))
            yield frames.event_frame(event)
            if event.indicator is None and frames.is_run_terminal(event.status):
                return
    finally:
        pending.cancel()
        # Let the cancellation land inside the generator so its own cleanup runs
        # before aclose() — closing one still suspended raises instead.
        with contextlib.suppress(BaseException):
            await pending
        with contextlib.suppress(Exception):
            await events.aclose()


@router.get("/{run_id}/events")
def stream_run_events(
    run_id: UUID,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> StreamingResponse:
    from reimagina.orchestration.factory import get_orchestrator

    if get_orchestrator().get_run(conn, str(run_id)) is None:
        raise HTTPException(status_code=404, detail=f"run {str(run_id)!r} not found")
    return StreamingResponse(_event_stream(str(run_id)), media_type="text/event-stream")
