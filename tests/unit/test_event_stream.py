"""Unit tests for the SSE route's streaming policy — no engine, no DB.

``_event_stream`` owns everything the *transport* must not: frame format,
keep-alive cadence, lifetime cap, and cleanup. A fake orchestrator supplies the
events, so these tests fail on route-policy regressions only.

Driven with ``asyncio.run`` rather than a pytest async plugin — the repo has
none, and one generator does not justify one.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from apps.api.routers import jobs
from reimagina.orchestration.port import RunEvent


class _FakeOrchestrator:
    """Yields a scripted event list. A None entry means "stall here forever"."""

    def __init__(self, script):
        self._script = script
        self.closed = False

    async def subscribe_progress(self, run_id):
        try:
            for event in self._script:
                if event is None:
                    await asyncio.Event().wait()  # never set — a quiet run
                yield event
        finally:
            self.closed = True


def _install(monkeypatch, orchestrator, *, heartbeat=0.02, max_seconds=5.0):
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator",
        lambda mode=None: orchestrator,
    )
    monkeypatch.setattr(jobs, "_HEARTBEAT_SECONDS", heartbeat)
    monkeypatch.setattr(jobs, "_STREAM_MAX_SECONDS", max_seconds)


def _drain(run_id="r1"):
    async def _run():
        return [chunk async for chunk in jobs._event_stream(run_id)]

    return asyncio.run(_run())


def _payloads(chunks):
    return [
        json.loads(c.removeprefix("data: ").strip())
        for c in chunks
        if c.startswith("data: ")
    ]


def test_terminal_snapshot_closes_the_stream_immediately(monkeypatch):
    """A run that finished before anyone subscribed must not hang the client."""
    fake = _FakeOrchestrator([RunEvent("r1", None, "succeeded")])
    _install(monkeypatch, fake)

    chunks = _drain()

    assert len(chunks) == 1
    assert chunks[0].startswith("data: ") and chunks[0].endswith("\n\n")
    assert _payloads(chunks) == [
        {"run_id": "r1", "indicator": None, "status": "succeeded"}
    ]
    assert fake.closed


def test_unit_events_are_framed_in_order_then_the_run_event_ends_it(monkeypatch):
    fake = _FakeOrchestrator(
        [
            RunEvent("r1", None, "running"),
            RunEvent("r1", "urban_form_block_compactness", "succeeded"),
            RunEvent("r1", "urban_form_block_density", "failed", "boom"),
            RunEvent("r1", None, "partial"),
            RunEvent("r1", "never", "reached"),
        ]
    )
    _install(monkeypatch, fake)

    payloads = _payloads(_drain())

    assert [p["indicator"] for p in payloads] == [
        None,
        "urban_form_block_compactness",
        "urban_form_block_density",
        None,
    ]
    assert payloads[-1]["status"] == "partial"
    assert fake.closed


def test_a_unit_level_terminal_status_does_not_end_the_stream(monkeypatch):
    """Only the run-level event closes it — a failed unit is not a failed run."""
    fake = _FakeOrchestrator(
        [
            RunEvent("r1", "a", "failed"),
            RunEvent("r1", "b", "succeeded"),
            RunEvent("r1", None, "partial"),
        ]
    )
    _install(monkeypatch, fake)

    assert len(_payloads(_drain())) == 3


def test_quiet_run_emits_keep_alives_and_still_delivers_the_next_event(monkeypatch):
    """The heartbeat must not consume the pending event it is waiting on.

    Timing out an in-flight ``__anext__`` would cancel it and leave the async
    generator unresumable — the keep-alive would break the very stream it
    exists to hold open.
    """

    async def _slow(run_id):
        await asyncio.sleep(0.1)
        yield RunEvent("r1", "a", "succeeded")
        yield RunEvent("r1", None, "succeeded")

    fake = _FakeOrchestrator([])
    fake.subscribe_progress = _slow
    _install(monkeypatch, fake, heartbeat=0.02)

    chunks = _drain()

    assert chunks.count(": keep-alive\n\n") >= 2
    payloads = _payloads(chunks)
    assert [p["indicator"] for p in payloads] == ["a", None]


def test_deadline_stops_a_run_that_never_finishes_and_closes_the_source(monkeypatch):
    fake = _FakeOrchestrator([RunEvent("r1", None, "running"), None])
    _install(monkeypatch, fake, heartbeat=0.02, max_seconds=0.15)

    chunks = _drain()

    assert _payloads(chunks)[0]["status"] == "running"
    assert chunks[-1] == ": keep-alive\n\n"
    assert fake.closed, "the subscription must be cleaned up when the route gives up"


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "cancelled"])
def test_every_terminal_run_status_closes_the_stream(monkeypatch, status):
    fake = _FakeOrchestrator([RunEvent("r1", None, status), RunEvent("r1", "x", "y")])
    _install(monkeypatch, fake)

    assert len(_payloads(_drain())) == 1
