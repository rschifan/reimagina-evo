"""What every Orchestrator implementation must do, regardless of engine.

These assertions are meant to read identically on every ops-plane branch. What
changes is ``conftest.IMPLEMENTATIONS`` — which orchestrators exist and what
they can do — so running this suite on two branches and diffing the two outputs
is the comparison, rather than a shared fixture neither branch could host (the
branches never merge).

Nothing here imports an engine, a queue, or a run table. It goes through
``factory.get_orchestrator`` and the port's four methods and nothing else; a
test that needs to know how a run is stored belongs in that engine's own suite.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from reimagina.orchestration.port import RUN_TERMINAL_STATES

from .conftest import HEALTHY_SLUG, POISONED_SLUG

_SUBSCRIBE_TIMEOUT = 10.0
_SETTLE_TIMEOUT = 120.0
"""Generous: a real unit is PostGIS over real geometry, not a stub."""

_CONVERGE_TIMEOUT = 20.0
"""For state the engine reaches shortly, not work it has to do."""


def _drain(agen) -> list:
    """Collect an async progress stream, failing rather than hanging."""

    async def _run():
        return await asyncio.wait_for(_collect(agen), timeout=_SUBSCRIBE_TIMEOUT)

    async def _collect(stream):
        return [event async for event in stream]

    return asyncio.run(_run())


def _make_terminal(orchestrator, conn, handle, impl):
    """Get a submitted run to a terminal state, however this engine allows.

    ⚠ The wait is load-bearing on an engine-owned plane. `cancel_run` returns
    once the engine has *accepted* the cancellation, not once the run is
    terminal — so reading the status immediately can still see `running`, and a
    caller that treated the call as synchronous would then cancel a second time
    and get True where it expected False. Where we own the tables the write has
    landed by the time the call returns.
    """
    if impl.blocking:
        return handle.status
    orchestrator.cancel_run(conn, handle.run_id)
    return _await_terminal(orchestrator, conn, handle.run_id)


def _await_terminal(orchestrator, conn, run_id, timeout=_CONVERGE_TIMEOUT):
    """Poll until the run stops changing, returning the last status seen."""
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = orchestrator.get_run(conn, run_id)
        if status is not None and status.status in RUN_TERMINAL_STATES:
            return status
        time.sleep(0.25)
    return status


def _settle(orchestrator, conn, run_id, timeout=_SETTLE_TIMEOUT):
    """Wait for a run to finish on its own. Fails loudly rather than hanging.

    Only meaningful where ``impl.settles`` — an implementation with no worker
    would sit here until the timeout and report a misleading failure.
    """
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = orchestrator.get_run(conn, run_id)
        if status is not None and status.status in RUN_TERMINAL_STATES:
            return status
        time.sleep(0.5)
    raise AssertionError(
        f"run {run_id} did not settle within {timeout}s "
        f"(last status: {status.status if status else None})"
    )


# ── Submit ──────────────────────────────────────────────────────────────────


def test_an_empty_dirty_set_is_a_noop_with_no_run_id(orchestrator, seeded):
    """Nothing was invalidated, so no run exists and no id may be advertised.

    Handing back an id here makes a client following the documented poll loop
    404 immediately after a *successful* submit.
    """
    handle = orchestrator.submit_run(
        seeded.conn,
        changed_attributes={"an_attribute_no_indicator_reads"},
        edited_features={seeded.block_id},
        code_version="conformance",
    )

    assert handle.status is not None
    assert handle.status.status == "noop"
    assert handle.status.units == ()
    assert orchestrator.get_run(seeded.conn, handle.run_id) is None


def test_a_submitted_run_is_retrievable_by_its_id(submit, orchestrator, seeded, impl):
    """The run must be findable, and must eventually report what it contains.

    ⚠ Two separate guarantees, and only the first is immediate on an
    engine-owned plane. `get_run` answers straight away, but the *units* are
    populated as the engine creates task rows — so a read microseconds after
    submit legitimately returns a run with `units=()`. Asserting both at once
    passed by luck and failed as soon as the machine was busy.

    Where we own the tables, `record_units` runs inside the submit transaction:
    a retrievable run always knows its own composition. That is the difference
    being measured, so the test states it rather than sleeping past it.
    """
    handle = submit()
    fetched = orchestrator.get_run(seeded.conn, handle.run_id)

    assert fetched is not None
    assert fetched.run_id == handle.run_id
    if not impl.blocking:
        fetched = _await_units(orchestrator, seeded.conn, handle.run_id)
    assert {u.indicator for u in fetched.units}


def _await_units(orchestrator, conn, run_id, timeout=_CONVERGE_TIMEOUT):
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = orchestrator.get_run(conn, run_id)
        if status is not None and status.units:
            return status
        time.sleep(0.25)
    return status


# ── Read ────────────────────────────────────────────────────────────────────


def test_an_unknown_run_is_none_not_an_error(orchestrator, seeded):
    assert (
        orchestrator.get_run(seeded.conn, "00000000-0000-0000-0000-000000000000")
        is None
    )


# ── Cancel ──────────────────────────────────────────────────────────────────


def test_cancelling_an_unknown_run_is_false(orchestrator, seeded):
    assert (
        orchestrator.cancel_run(seeded.conn, "00000000-0000-0000-0000-000000000000")
        is False
    )


def test_cancelling_a_live_run_closes_it(submit, orchestrator, seeded, impl):
    """Cancel must leave the run terminal, not merely stop scheduling.

    ⚠ The return value is deliberately not asserted to be True. Whether the run
    is still live a moment after submit is a race the suite does not control:
    against a local worker and a one-block AOI the whole run can finish in well
    under a second, and `cancel_run` then correctly returns False because there
    was nothing left to cancel. Asserting True would make this test a
    measurement of how fast the machine is.

    What must hold either way is the postcondition — the run is terminal
    afterwards. `test_cancelling_a_finished_run_is_false` covers the False case
    deliberately rather than by luck.
    """
    if impl.blocking:
        pytest.skip(f"{impl.mode!r} finishes the run before submit_run returns")

    handle = submit()
    orchestrator.cancel_run(seeded.conn, handle.run_id)

    fetched = orchestrator.get_run(seeded.conn, handle.run_id)
    assert fetched is not None
    assert fetched.status in RUN_TERMINAL_STATES


def test_cancelling_a_finished_run_is_false(submit, orchestrator, seeded, impl):
    """Idempotent: a second cancel, or one after completion, changes nothing."""
    handle = submit()
    _make_terminal(orchestrator, seeded.conn, handle, impl)

    assert orchestrator.cancel_run(seeded.conn, handle.run_id) is False


# ── Subscribe ───────────────────────────────────────────────────────────────


def test_subscribing_to_a_terminal_run_yields_then_stops(
    submit, orchestrator, seeded, impl
):
    """A late subscriber must be told the outcome and released, not left waiting.

    The first event is always the snapshot, so this holds no matter how long
    after the run the subscription opens.
    """
    handle = submit()
    _make_terminal(orchestrator, seeded.conn, handle, impl)

    events = _drain(orchestrator.subscribe_progress(handle.run_id))

    assert events, "a subscription must yield at least the snapshot"
    assert events[0].run_id == handle.run_id
    assert events[0].indicator is None  # run-level: the event that closes a stream
    assert events[0].status in RUN_TERMINAL_STATES


# ── Partial success ─────────────────────────────────────────────────────────


def test_one_failed_unit_does_not_fail_its_independent_siblings(
    poison, submit, orchestrator, seeded, impl
):
    """F6. The unit that failed is the only direct casualty.

    This is the assertion most likely to differ across ops-planes: an engine
    that owns run state may have no vocabulary for a run that half-worked, which
    is why the expected rollup is declared per implementation rather than
    written here.
    """
    if not impl.settles:
        pytest.skip(f"{impl.mode!r} cannot reach a failure without a worker")

    handle = submit()
    fetched = (
        handle.status
        if impl.blocking
        else _settle(orchestrator, seeded.conn, handle.run_id)
    )
    assert fetched is not None

    by_slug = {u.indicator: u.status for u in fetched.units}
    assert by_slug[POISONED_SLUG] == "failed"
    assert by_slug[HEALTHY_SLUG] == "succeeded"
    assert fetched.status == impl.failed_run_status


def test_a_composite_is_skipped_when_a_dirty_input_fails(
    poison, submit, orchestrator, seeded, impl
):
    """A composite cannot be trusted when an input it depends on failed.

    Skipped, not failed: it never ran. The distinction is the one an
    engine-owned status model is most likely to lose.
    """
    if not impl.settles:
        pytest.skip(f"{impl.mode!r} cannot reach a failure without a worker")

    handle = submit()
    fetched = (
        handle.status
        if impl.blocking
        else _settle(orchestrator, seeded.conn, handle.run_id)
    )
    assert fetched is not None

    composites = [
        u for u in fetched.units if u.indicator not in (POISONED_SLUG, HEALTHY_SLUG)
    ]
    assert composites, "the shipped DAG must have a downstream unit to skip"
    assert all(u.status == impl.skipped_unit_status for u in composites)
