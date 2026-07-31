"""Shared helpers for the suites that need a real Hatchet engine.

Three conftests need the same two things — a consistent rule for when a missing
engine is a skip rather than a failure, and a way to stop a worker through SDK
internals — and each had its own copy. Both encode policy rather than
convenience, which is what makes duplication expensive here: the skip rule *is*
the fix for a CI job that passed green having run nothing, and the teardown
accessor *is* the workaround for an SDK with no public stop API. A second copy
is a second place for either to silently stop being true.
"""

from __future__ import annotations

import os
import subprocess

import pytest


def absent_engine(reason: str) -> None:
    """Skip when there is genuinely no engine; fail when one was asked for.

    ⚠ The asymmetry is the whole point. Every gate in these suites used to be an
    unconditional `pytest.skip`, and pytest exits 0 when everything skips — so
    CI could stand an engine up, mint a broken token, register nothing and
    report success having executed zero engine assertions. That is precisely the
    silent green the engine job exists to end, reproduced inside it.

    No token means someone is running the fast suites locally and these tests
    are not what they asked for: skip. A token means they were asked for, and
    not running them is a failure.
    """
    if os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.fail(f"engine tests were requested but could not run: {reason}")
    pytest.skip(f"HATCHET_CLIENT_TOKEN unset — {reason}")


def worker_internal(worker: object, name: str) -> object | None:
    """Read an SDK-internal worker attribute, private backing field first.

    ⚠ There is no public teardown API. `worker.loop` and
    `worker.action_listener_process` are `@property` shims documented "will be
    removed in v2.0.0" — the same objection this branch raised to justify moving
    off `ctx.exit_flag`, so calling them public was wrong.

    ⚠ Private **first**, deliberately. Reading the public shim emits a
    `DeprecationWarning`, and silencing that needs `warnings.catch_warnings()`,
    which swaps a process-global filter — unsafe here because the conformance
    worker runs in a daemon thread that emits warnings of its own while teardown
    runs on the main thread. Going to the backing field directly raises no
    warning, so no global state has to be touched at all.

    The public property remains the fallback: if a future SDK renames the
    private field, the shim is what is left.

    ⚠ Callers must not assume `.is_alive()` exists on what comes back. An
    earlier note here claimed `durable_action_listener_process` returns the
    durable *event queue*; it does not — at 1.35.1 it returns
    `_durable_action_listener_process`, a `BaseProcess | None`. The guard stays
    because the attribute set is internal and may not stay process-shaped, but
    the reason given for it was wrong.
    """
    private = getattr(worker, f"_{name}", None)
    return private if private is not None else getattr(worker, name, None)


def stop_worker(worker: object, timeout: float = 20.0, log=None) -> None:
    """Stop a worker so the pytest session can exit.

    ⚠ **`exit_gracefully` is `async def`.** Called from sync code it returns a
    coroutine nobody awaits — Python only warns — so it does nothing whatsoever.
    Awaited properly it deregisters: its first act is `_pause_task_assignment()`,
    "tell the engine that the worker is paused". That single mistake is the
    whole of the "a clean exit leaks a registration like a SIGKILL does"
    finding, and the reason a setup-time reaper was written to compensate.

    ⚠ The listener is a spawned child process and is not a daemon, so Python
    joins it at interpreter exit: a listener that never stops hangs the session
    *after the last test passed*, with no output and no failing test. Killing it
    is a bounded safety net, not the mechanism.
    """
    import asyncio

    loop = worker_internal(worker, "loop")
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(worker.exit_gracefully(), loop).result(
                timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 — teardown must not mask a failure
            if log is not None:
                log.warning("graceful worker stop failed: %s", exc)

    for attr in ("action_listener_process", "durable_action_listener_process"):
        proc = worker_internal(worker, attr)
        if proc is not None and hasattr(proc, "is_alive") and proc.is_alive():
            if log is not None:
                log.warning("%s survived graceful stop; terminating", attr)
            proc.terminate()
            proc.join(timeout=5.0)


def kill_process_group(proc: subprocess.Popen, *, timeout: float = 15.0) -> None:
    """`SIGKILL` a worker's whole process group, not just its parent.

    ⚠ The single most important operational fact on this track. `proc.kill()`
    signals one pid; the SDK's action listener is a **spawned child process**,
    so it survives, is reparented to init, keeps its gRPC stream open and keeps
    heartbeating. The engine then correctly reports the worker ACTIVE — something
    really is alive claiming to be it — and keeps assigning work the orphan can
    no longer execute, which presents as runs sitting at `running` with no units.

    Measured: 27 such orphans had accumulated on one machine, the oldest over a
    day old. Killing them dropped the engine's ACTIVE count to zero at once.

    ⚠ The group id must be recorded while the parent is alive; `os.getpgid`
    raises once it is reaped, and a fallback to `proc.kill()` cannot reach an
    orphan.
    """
    import signal

    pgid = getattr(proc, "_pgid", None)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
