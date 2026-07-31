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
import os
import time
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..deps import api_dsn

_STREAM_MAX_SECONDS = 300.0
_HEARTBEAT_SECONDS = 15.0

_OUTAGE_DEADLINE_SECONDS = float(
    os.environ.get("REIMAGINA_SSE_OUTAGE_DEADLINE_S", "60")
)
"""How long a *continuous* listener outage may last before the stream gives up.

Bounded rather than unlimited: a client waiting forever on a stream that will
never recover is worse than one told plainly that it ended. Reset by any
successful resubscription, so unrelated blips over a long run do not accumulate
into a spurious failure.
"""

_RECONNECT_BACKOFF_SECONDS = 1.0
_RECONNECT_BACKOFF_MAX_SECONDS = 8.0
"""Capped so a long outage still retries often enough to catch the recovery."""

_RECONNECT_BACKOFF_MIN_SECONDS = 0.05
"""⚠ A floor, because a zero wait is a hot loop, not a fast retry.

Tests set the backoff to 0 to keep themselves quick, and `0 * 2 == 0` — so a
persistent outage would spin on the event loop emitting a keep-alive per tick
for the whole outage deadline, starving everything else on the worker.
"""


async def _backoff(seconds: float) -> None:
    """Sleep between reconnect attempts, bounded at both ends."""
    await asyncio.sleep(
        min(
            max(seconds, _RECONNECT_BACKOFF_MIN_SECONDS), _RECONNECT_BACKOFF_MAX_SECONDS
        )
    )


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
def get_run(run_id: UUID) -> RunView:
    """⚠ No `Depends(get_db_conn)`. Reading a run's status touches the engine
    and nothing else, so holding a pooled connection for the request was a
    connection spent on nothing — and under load the pool is the scarce thing.

    ⚠ ``run_id: UUID``, not ``str``. A run id *is* a uuid, and typing it as
    one rejects a malformed id here, with 422, instead of letting it reach the
    engine client — whose own length validation raises a pydantic error carrying
    no HTTP status, which the orchestrator then cannot tell from an outage and
    reports as a retryable 503. Validating at the boundary deletes that whole
    special case."""
    from reimagina.orchestration.factory import get_orchestrator

    status = get_orchestrator().get_run(str(run_id))
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
    wire — frame format, keep-alive cadence, the lifetime cap, and how long to
    keep trying. Where the events come from (LISTEN/NOTIFY, a workflow stream,
    a websocket) is not this module's business.

    ⚠ A listener interruption is not the end of the run. The engine's progress
    stream is a gRPC connection and it drops for ordinary reasons — an engine
    restart, a rolling deploy, a network blip — while the run continues
    perfectly well. Ending the stream on the first of those told the client its
    run had failed to be observable, about a run that was still executing.

    So it reconnects, with bounded backoff, until the run reaches a terminal
    state or `_OUTAGE_DEADLINE_SECONDS` of *continuous* failure. Reconnection
    reconciles for free: `subscribe_progress` emits a snapshot of current status
    first, so a client that missed events while disconnected is caught up rather
    than left with a stale picture. The error frame is emitted only once that
    budget is exhausted.
    """
    from reimagina.orchestration.factory import get_orchestrator

    deadline = time.monotonic() + _STREAM_MAX_SECONDS
    outage_deadline: float | None = None
    backoff = _RECONNECT_BACKOFF_SECONDS
    # What has already reached the client. A reconnect replays the snapshot, and
    # re-sending a unit's status is noise the client has to de-duplicate itself.
    emitted: dict[str | None, str] = {}

    def subscribe():
        return get_orchestrator().subscribe_progress(run_id)

    while time.monotonic() < deadline:
        # ⚠ Failing to *open* a subscription and losing one mid-stream take the
        # same path, because they are the same outage from the client's side.
        # Splitting them left the retry policy written out twice — and the
        # budget reset added to one copy applied only there, which is the drift
        # `_give_up_or_wait` was extracted to stop and only half-stopped.
        interrupted, delivered = False, False
        async for frame in _drain(subscribe, deadline, emitted):
            if frame is _INTERRUPTED:
                interrupted = True
                break
            # ⚠ Keep-alives do not count. `_drain` emits one every
            # `_HEARTBEAT_SECONDS`, and treating it as delivery meant a listener
            # that opened, produced nothing for 16s and dropped reset its own
            # budget every round — the "never gives up" mode this flag exists to
            # avoid, hanging the request for the full stream lifetime.
            if not frame.startswith(":"):
                delivered = True
            yield frame

        if not interrupted:
            return

        # ⚠ Reset on a subscription that delivered a frame, not on one that
        # merely opened. Both neighbouring mistakes have been made here:
        #
        #   never reset — the first blip pinned the deadline forever, so an
        #     unrelated blip minutes later found it long expired and killed a
        #     healthy stream with zero retries.
        #   reset on every attempt — a listener that opens and immediately drops
        #     clears its own budget each round, so the loop never gives up and
        #     the request hangs for the full `_STREAM_MAX_SECONDS`.
        #
        # ⚠ `delivered` is set *before* de-duplication, deliberately. Keying it
        # on frames that survived the `emitted` filter would mean a reconnect
        # replaying only already-seen statuses did not count as recovery — the
        # comment would say "delivered something" while the code meant
        # "delivered something new", and a stable run reconnecting mid-flight
        # would burn its budget while perfectly healthy.
        if delivered:
            outage_deadline = None
            backoff = _RECONNECT_BACKOFF_SECONDS

        async for frame in _give_up_or_wait(outage_deadline, backoff):
            yield frame
        if outage_deadline is not None and time.monotonic() >= outage_deadline:
            return
        outage_deadline = outage_deadline or time.monotonic() + _OUTAGE_DEADLINE_SECONDS
        backoff *= 2


async def _give_up_or_wait(
    outage_deadline: float | None, backoff: float
) -> AsyncIterator[str]:
    """Emit the outage frame if the budget is spent, else keep alive and wait.

    Kept separate from the loop so "what a client sees during an outage" is one
    readable thing rather than an else-branch. There is now only one caller —
    the second arm it was extracted for is gone, because `_drain` reports a
    failed subscription the same way it reports a dropped one.
    """
    from reimagina.orchestration import frames

    if outage_deadline is not None and time.monotonic() >= outage_deadline:
        yield frames.error_frame("orchestrator_unavailable")
        return
    yield ": keep-alive\n\n"
    await _backoff(backoff)


# ⚠ No `_TERMINAL` sentinel. It signalled "the run finished", but the caller
# already treats *anything except an interruption* as a finished stream, so the
# branch, the two yield sites and the sentinel itself carried no information.
_INTERRUPTED = object()
"""The listener failed: the caller decides whether to reconnect or give up."""


async def _drain(subscribe, deadline: float, emitted: dict) -> AsyncIterator[object]:
    """One subscription's worth of frames, ending in a sentinel.

    Split out so the reconnect loop above reads as policy rather than as two
    nested error paths. Yields SSE strings, then either `_INTERRUPTED` or
    nothing — ending without it means the stream finished normally, whether the
    run reached a terminal event or the overall deadline passed.

    ⚠ Opens the subscription too, rather than taking one already opened. A
    failure to subscribe is an outage exactly like a mid-stream drop, so it
    reports the same sentinel and the caller keeps one retry policy instead of
    two copies of it.
    """
    from reimagina.orchestration import frames

    try:
        events = subscribe().__aiter__()
    except Exception:  # noqa: BLE001 — the caller decides; see below
        yield _INTERRUPTED
        return

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
                # The orchestrator closed the stream without a terminal event.
                # Nothing failed, so this is not an outage — stop.
                return
            except Exception:  # noqa: BLE001 — see below; silence is worse
                # ⚠ The response is already 200 with its headers on the wire, so
                # the registered 503 handler cannot run: Starlette raises
                # "response already started" rather than replacing it. Letting
                # the exception escape therefore just stops the stream, which a
                # client cannot distinguish from a completed run.
                #
                # Broad on purpose, and engine-agnostic on purpose: whatever
                # went wrong reading progress, this module names no engine.
                # Normal termination is `StopAsyncIteration`, caught above; a
                # client disconnect is `CancelledError`, a BaseException, and
                # still propagates.
                #
                # Hands back to the reconnect loop rather than ending the
                # stream: the run is very likely still executing, and a dropped
                # gRPC listener is not a failed run.
                yield _INTERRUPTED
                return
            pending = asyncio.ensure_future(anext(events))

            # ⚠ Keyed on what the frame actually carries, and holding only the
            # *last* status per unit. Including `detail` — which
            # `frames.event_frame` does not write — both missed real duplicates
            # (two events differing only in detail produce identical frames) and
            # suppressed real progress: a retried unit walking
            # running -> failed -> running -> succeeded had its second `running`
            # dropped, so a client never learned the retry began. Comparing
            # against the previous state per indicator collapses a reconnect's
            # replayed snapshot without hiding a genuine transition back.
            if emitted.get(event.indicator) != event.status:
                emitted[event.indicator] = event.status
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
def stream_run_events(run_id: UUID) -> StreamingResponse:
    """⚠ No `Depends(get_db_conn)`, and here it mattered most: the dependency
    was held for the **lifetime of the stream** — up to `_STREAM_MAX_SECONDS` —
    to serve one existence check that never touched the database.
    """
    from reimagina.orchestration.factory import get_orchestrator

    if get_orchestrator().get_run(str(run_id)) is None:
        raise HTTPException(status_code=404, detail=f"run {str(run_id)!r} not found")
    return StreamingResponse(_event_stream(str(run_id)), media_type="text/event-stream")
