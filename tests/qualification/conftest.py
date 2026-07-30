"""Engine access for the qualification suite.

Deliberately thin: these tests are about the engine's own behaviour, so they
talk to it directly rather than through the port. Nothing here starts a
session-scoped worker — the point of several of these is to watch a worker
*appear and disappear*, which a shared one would hide.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

WORKER_PREFIX = "qualification"
"""Every worker this suite starts is named with this prefix.

So an assertion can say "of the workers *this suite* created" without reasoning
about the `conformance`, `reimagina-worker` and `workload-bench` registrations
that share the engine.
"""


@pytest.fixture(scope="session")
def engine():
    """The Hatchet client, or a skip.

    Same gate as the conformance suite: no token, no engine, no claim. A
    qualification result that silently did not run is worse than none.
    """
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.skip("HATCHET_CLIENT_TOKEN unset — nothing to qualify against")

    from hatchet_sdk import Hatchet

    try:
        client = Hatchet()
        client.workers.list()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"engine unreachable: {exc}")
    return client


def workers_named(engine, prefix: str = WORKER_PREFIX) -> list:
    """Registrations whose name starts with `prefix`, newest heartbeat first."""
    rows = getattr(engine.workers.list(), "rows", None) or []
    mine = [r for r in rows if str(getattr(r, "name", "") or "").startswith(prefix)]
    return sorted(
        mine,
        key=lambda r: getattr(r, "last_heartbeat_at", None)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def is_active(row) -> bool:
    """`WorkerStatus.ACTIVE` — and *not* `INACTIVE`, which also ends in it."""
    return str(getattr(row, "status", "")).rsplit(".", 1)[-1] == "ACTIVE"


def wait_until(predicate, *, timeout: float, interval: float = 1.0) -> bool:
    """Poll until true or the budget runs out. Returns whether it became true.

    Returning rather than asserting so a caller can report *how long* it took,
    which is the number that matters for an expiry window.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def worker_process(engine, migrated_db):
    """Start `python -m apps.worker` as a real child process, and clean up.

    A subprocess, not an in-process thread, because two of these tests need to
    kill a worker in a way it cannot handle — `SIGKILL` has no meaning for a
    daemon thread, and a thread that leaks keeps heartbeating from inside the
    test session, which would answer the question wrongly.

    ⚠ Depends on `migrated_db`, and passes its DSN down explicitly. The worker
    refuses to start against an unmigrated database — correctly, that preflight
    is the point of Phase 7 — and the shared fixture downgrades to base on
    teardown, so whether `reimagina_test` has tables depends on whether a suite
    is holding it. Inheriting the ambient `DATABASE_URL_DIRECT` made this suite
    skip with "never registered as ACTIVE", which reads like an engine problem
    and is not one.
    """
    started: list[subprocess.Popen] = []

    def _start(name: str, slots: int = 2) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "apps.worker",
                "--name",
                name,
                "--slots",
                str(slots),
            ],
            env={**os.environ, "DATABASE_URL_DIRECT": migrated_db},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # ⚠ Its own process group, so a test can kill the *whole worker*.
            # The SDK runs its action listener in a spawned child process, and
            # signalling only the parent leaves that child orphaned, still
            # holding its gRPC stream and still heartbeating. See
            # `kill_process_group`.
            start_new_session=True,
        )
        # ⚠ Record the group id now, while the parent is alive. After it is
        # reaped `os.getpgid(proc.pid)` raises, and cleanup that falls back to
        # `proc.kill()` cannot reach an orphaned listener — which is exactly the
        # state the orphan test leaves behind on purpose. Without this the suite
        # leaks the very processes it exists to characterise.
        proc._pgid = os.getpgid(proc.pid)  # noqa: SLF001 — our own Popen
        started.append(proc)
        registered = wait_until(
            lambda: any(
                getattr(r, "name", None) == name and is_active(r)
                for r in workers_named(engine)
            ),
            timeout=60.0,
        )
        if not registered:
            proc.kill()
            # ⚠ Report why. A bare "never registered" skip is indistinguishable
            # from an engine that cannot register workers — which is the thing
            # this suite exists to judge — when the actual cause is usually the
            # worker refusing to start for a reason it printed.
            _, err = proc.communicate(timeout=10)
            detail = (err or b"").decode(errors="replace").strip().splitlines()
            pytest.skip(
                f"worker {name!r} never registered as ACTIVE; "
                f"last stderr: {detail[-1] if detail else '(none)'}"
            )
        return proc

    yield _start

    for proc in started:
        kill_process_group(proc)


def kill_process_group(proc: subprocess.Popen, *, timeout: float = 15.0) -> None:
    """`SIGKILL` the worker's whole process group, not just its parent.

    ⚠ This distinction is the single most important operational fact on this
    track. `proc.kill()` signals one pid; the SDK's action listener is a
    **spawned child process**, so it survives, is reparented to init, keeps its
    gRPC stream open and **keeps heartbeating**. The engine then correctly
    reports the worker ACTIVE — something really is alive claiming to be it —
    and keeps assigning work that the orphan can no longer execute, which
    presents as runs sitting at `running` with no units.

    Measured: 27 such orphans had accumulated on this machine, the oldest over
    a day old, each holding a registration open. Killing them dropped the
    engine's ACTIVE count to zero immediately, with no change to the engine.

    A container runtime or systemd kills the whole group, which is why this is
    a deployment requirement rather than a code one — and why the test must do
    what the deployment does, or it measures a scenario that cannot happen in
    production while missing the one that can.
    """
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
