"""The subscriber slot must be held by the pump thread, not by the generator.

``read_stream`` is a synchronous generator that blocks until the run's stream
closes, and nothing can interrupt it. ``subscribe_progress`` therefore runs it
on a dedicated daemon thread and caps how many may exist at once — the cap is
the only thing standing between a busy SSE endpoint and an unbounded pile of
uninterruptible threads.

The cap only does that if it is released where the thread ends. Released when
the *generator* ends — which is what it did — it counts live subscriptions
instead, and those are a different number for precisely the reason the cap
exists: a client that disconnects one second into a twenty-minute run ends the
generator immediately while its pump stays blocked for the remaining nineteen
minutes. The freed slot admits the next subscriber, which starts another
thread, and so on with no bound at all.

Driven with ``asyncio.run`` rather than a pytest async plugin — the repo has
none, and two generators do not justify one.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from reimagina.orchestration import dbos_orchestrator


class _BlockingClient:
    """A stream that hands over one event, then blocks like the real one does."""

    def __init__(self, release: threading.Event, entered: threading.Event) -> None:
        self._release = release
        self._entered = entered

    def read_stream(self, run_id, stream):
        yield {"run_id": run_id, "indicator": "a", "status": "running"}
        self._entered.set()
        self._release.wait(timeout=5)


def _subscriber(monkeypatch, release, entered, slots):
    monkeypatch.setattr(dbos_orchestrator, "_PROGRESS_SLOTS", slots)
    monkeypatch.setattr(
        dbos_orchestrator, "_client", lambda: _BlockingClient(release, entered)
    )
    orchestrator = dbos_orchestrator.DBOSOrchestrator(
        registry=(), execution_mode="client"
    )
    # No envelope yet: the generator skips the snapshot and goes straight to the
    # stream, which is the path a live subscriber takes.
    monkeypatch.setattr(orchestrator, "_snapshot", lambda run_id: None)
    return orchestrator


def test_the_slot_outlives_the_generator_and_ends_with_the_thread(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    slots = threading.BoundedSemaphore(1)
    orchestrator = _subscriber(monkeypatch, release, entered, slots)

    async def _subscribe_then_disconnect():
        events = orchestrator.subscribe_progress("run-1").__aiter__()
        first = await anext(events)
        # The client goes away mid-run, exactly as an SSE disconnect does.
        await events.aclose()
        return first

    try:
        first = asyncio.run(_subscribe_then_disconnect())
        assert first.indicator == "a"

        # The pump is still inside read_stream — it cannot be woken. The slot it
        # holds must still be taken, or the next subscriber starts a second
        # uninterruptible thread while this one is alive.
        assert entered.wait(timeout=5), "pump never reached the blocking read"
        assert not slots.acquire(blocking=False), (
            "slot was released while the pump thread was still blocked — "
            "the cap is counting subscriptions, not threads"
        )
    finally:
        release.set()

    # Once the stream ends, the thread ends, and the slot comes back.
    deadline = threading.Event()
    for _ in range(50):
        if slots.acquire(blocking=False):
            slots.release()
            break
        deadline.wait(0.05)
    else:  # pragma: no cover - only on a regression
        pytest.fail("slot was never released after the pump thread ended")


def test_capacity_is_refused_rather_than_queued(monkeypatch):
    """A full cap is a reconnect, not a wait — the caller is an HTTP request."""
    release = threading.Event()
    entered = threading.Event()
    slots = threading.BoundedSemaphore(1)
    orchestrator = _subscriber(monkeypatch, release, entered, slots)

    async def _two_subscribers():
        first = orchestrator.subscribe_progress("run-1").__aiter__()
        await anext(first)
        assert entered.wait(timeout=5)
        second = orchestrator.subscribe_progress("run-2").__aiter__()
        with pytest.raises(RuntimeError, match="capacity"):
            await anext(second)
        await first.aclose()

    try:
        asyncio.run(_two_subscribers())
    finally:
        release.set()


def test_the_pump_releases_the_semaphore_it_acquired(monkeypatch):
    """Not whatever the module global points at by the time it finishes.

    The pump runs on a daemon thread that deliberately outlives the generator,
    so it can reach its `finally` long after the call that started it has
    returned. Resolving `_PROGRESS_SLOTS` there rather than capturing it meant
    releasing a *different* object if anything had rebound the name in
    between — which is not hypothetical, because that is precisely what the
    test above does, and what a second worker process configuring its own cap
    would do.

    The symptom was not a leaked slot. `BoundedSemaphore.release` raises when
    the count is already full, and it raised *inside* the `finally` — so the
    `done` sentinel queued after it never reached the subscriber, and an SSE
    client waited for a stream end that never came.
    """
    real = dbos_orchestrator._PROGRESS_SLOTS
    acquired = threading.BoundedSemaphore(1)
    release = threading.Event()
    entered = threading.Event()
    orchestrator = _subscriber(monkeypatch, release, entered, acquired)

    async def _subscribe_then_disconnect():
        events = orchestrator.subscribe_progress("run-1").__aiter__()
        await anext(events)
        await events.aclose()

    asyncio.run(_subscribe_then_disconnect())
    assert entered.wait(timeout=5), "pump never reached the blocking read"

    # The cap is rebound while the pump is still blocked, exactly as
    # monkeypatch teardown does when a test returns before its pump wakes.
    monkeypatch.setattr(dbos_orchestrator, "_PROGRESS_SLOTS", real)
    release.set()

    for _ in range(100):
        if acquired.acquire(blocking=False):
            acquired.release()
            break
        threading.Event().wait(0.05)
    else:  # pragma: no cover - only on a regression
        pytest.fail("the pump did not release the semaphore it acquired")

    # And the rebound one was left alone: releasing a full BoundedSemaphore
    # raises, so this is what the regression actually looked like.
    assert real.acquire(blocking=False), "the real cap was disturbed"
    real.release()
