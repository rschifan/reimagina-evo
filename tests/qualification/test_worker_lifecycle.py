"""Does the engine manage worker liveness, or do we have to?

The acceptance criterion is narrow and load-bearing: **a dead worker must stop
receiving assignments, without us doing anything about it.** If that does not
hold, the answer is a different engine release or no Track B — not a worker
registry of our own, because the claim being tested is precisely that a service
track does not need one.

It holds. The engine keeps a registration ACTIVE exactly as long as something
heartbeats for it, which is the correct rule. What made this look broken was on
our side of the wire, and it is the finding these tests exist to pin: **the
worker is a process tree, and killing its root does not kill it.**
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from tests.qualification.conftest import (
    is_active,
    kill_process_group,
    wait_until,
    workers_named,
)

EXPIRY_BUDGET_S = 180.0
"""How long a dead worker may remain eligible before this is a real hazard."""


def _still_active(engine, name: str) -> bool:
    return any(
        getattr(r, "name", None) == name and is_active(r) for r in workers_named(engine)
    )


def test_a_worker_that_exits_cleanly_stops_being_eligible(worker_process, engine):
    """The ordinary case, and the one previously believed broken.

    The branch recorded that `exit_gracefully()` "does not deregister" and that
    a clean exit leaks exactly like a kill. It does deregister: its first act is
    `_pause_task_assignment()` — *"tell the engine that the worker is paused"*.
    The earlier result came from calling an `async def` from sync code, where it
    returned a coroutine nobody awaited and did nothing at all.
    """
    name = f"qualification-clean-{uuid.uuid4().hex[:8]}"
    proc = worker_process(name)

    proc.terminate()  # SIGTERM — the signal a container runtime sends first
    proc.wait(timeout=60)

    assert wait_until(
        lambda: not _still_active(engine, name), timeout=EXPIRY_BUDGET_S
    ), f"{name} still ACTIVE after a clean exit"


def test_killing_the_whole_process_group_ends_the_registration(worker_process, engine):
    """`SIGKILL` to the group: no graceful path, nothing we can run.

    This is the one that decides whether the engine owns liveness — a clean
    exit could always have been handled in application code, a killed process
    cannot. The group is what a container runtime or systemd signals, so it is
    what the test must signal.
    """
    name = f"qualification-killed-{uuid.uuid4().hex[:8]}"
    proc = worker_process(name)

    kill_process_group(proc)

    started = time.monotonic()
    expired = wait_until(
        lambda: not _still_active(engine, name), timeout=EXPIRY_BUDGET_S
    )
    elapsed = time.monotonic() - started

    assert expired, (
        f"{name} was still ACTIVE {EXPIRY_BUDGET_S:.0f}s after the whole process "
        f"group was killed. The engine cannot own liveness — escalate to a newer "
        f"engine release rather than writing a reaper."
    )
    print(f"\nregistration cleared after group SIGKILL: ~{elapsed:.0f}s")


def test_killing_only_the_parent_leaves_a_heartbeating_orphan(worker_process, engine):
    """⚠ The deployment requirement, asserted as the hazard it is.

    This documents a failure rather than a guarantee, and it is deliberate.

    The SDK runs its action listener in a **spawned child process**. Signal only
    the parent and that child survives, is reparented to init, keeps its gRPC
    stream open and **keeps heartbeating** — so the registration stays ACTIVE,
    correctly, because something really is alive claiming to be that worker. The
    engine keeps assigning it work the orphan can no longer execute, and runs
    sit at `running` with no units.

    That is the whole of the "stale workers never expire" finding. It was never
    engine behaviour. Measured on this machine: 27 orphans accumulated, oldest
    over a day; killing them dropped the engine's ACTIVE count to zero at once.

    **So the worker must be supervised as a process tree** — its own container
    with the worker as PID 1, or a supervisor that signals the group. If this
    test ever starts failing, the SDK has changed its process model and the
    deployment note in the runbook can be relaxed.
    """
    name = f"qualification-orphan-{uuid.uuid4().hex[:8]}"
    proc = worker_process(name)

    proc.kill()  # the parent only — deliberately the wrong thing
    proc.wait(timeout=60)

    # Give the engine longer than any plausible expiry: the point is that it
    # does *not* expire, because the orphan is still heartbeating.
    time.sleep(45)

    rows = [r for r in workers_named(engine) if getattr(r, "name", None) == name]
    assert rows, f"{name} vanished from the registry entirely"
    row = rows[0]
    heartbeat_age = (
        datetime.now(timezone.utc) - getattr(row, "last_heartbeat_at")
    ).total_seconds()

    assert is_active(row) and heartbeat_age < 45, (
        f"expected an orphaned listener to keep {name} alive, but it is "
        f"{getattr(row, 'status', None)} with a {heartbeat_age:.0f}s old "
        f"heartbeat. If the SDK stopped spawning a child listener, delete this "
        f"test and the process-group requirement in the runbook."
    )

    kill_process_group(proc)  # the fixture would too; be explicit


def test_a_replacement_worker_registers_alongside_expired_ones(worker_process, engine):
    """A restart is the deployment case, and must not need the old one cleared.

    Registrations are append-only history, so a replacement always starts in a
    registry that still lists its predecessors. If a stale row blocked or
    diverted work, rolling deploys would need an application-side cleanup step.
    """
    first = f"qualification-replaced-{uuid.uuid4().hex[:8]}"
    kill_process_group(worker_process(first))

    second = f"qualification-replacement-{uuid.uuid4().hex[:8]}"
    worker_process(second)

    assert _still_active(engine, second), "the replacement did not register"


def test_no_registration_is_eligible_long_after_its_last_heartbeat(engine):
    """The property the deleted reaper was compensating for.

    Runs of this branch's suites leave registrations behind — they are history
    and are never deleted. What made that a hazard was the belief they stayed
    ACTIVE and kept being handed work. Whatever has accumulated, nothing whose
    heartbeat stopped should still be eligible.

    ⚠ Asserts over *every* worker the engine lists, not just this suite's. A
    live worker from another suite is legitimate and will be ACTIVE with a fresh
    heartbeat; what must not exist is an ACTIVE worker that stopped reporting.
    """
    rows = getattr(engine.workers.list(), "rows", None) or []
    now = datetime.now(timezone.utc)

    stale_but_eligible = [
        (getattr(r, "name", None), (now - hb).total_seconds())
        for r in rows
        if is_active(r)
        and (hb := getattr(r, "last_heartbeat_at", None)) is not None
        and (now - hb).total_seconds() > EXPIRY_BUDGET_S
    ]

    assert not stale_but_eligible, (
        f"registrations still ACTIVE long after their last heartbeat: "
        f"{stale_but_eligible}. Either the engine stopped expiring workers, or "
        f"orphaned listener processes are still heartbeating for them — check "
        f"for `spawn_main` processes with PPID 1 before blaming the engine."
    )
