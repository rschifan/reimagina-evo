"""A failed beat must not end the reporter.

``heartbeat()`` opens a short direct connection, so a failover, a restart or a
momentarily full pool raises inside the loop thread. Unhandled, that ends the
thread while the worker keeps dequeuing and executing units — the row in
``jobs_runtime.ops_executor_heartbeat`` simply stops advancing, with nothing
logged. "Stale but very much alive" is the one input the cross-executor sweep's
liveness gate must never be handed, and that gate exists precisely because
``dbos>=2.28,<3`` is pinned below the idempotent-recovery fix.

Driven through the real ``run_dbos_worker`` with a real thread rather than by
calling the closure directly: the loop, its interval, its ``stop`` event and its
error handling only exist together, and a test that reached inside would pass
against a version where they had come apart.
"""

from __future__ import annotations

import threading

import pytest

from apps.worker import app as worker_app


class _FlakyHeartbeat:
    """Fails on the beats named in ``fail_on``, counting every attempt."""

    def __init__(self, fail_on: set[int], enough: threading.Event, target: int) -> None:
        self.calls = 0
        self._fail_on = fail_on
        self._enough = enough
        self._target = target

    def __call__(self) -> None:
        self.calls += 1
        if self.calls >= self._target:
            self._enough.set()
        if self.calls in self._fail_on:
            raise RuntimeError(f"connection refused (beat {self.calls})")


def _run_worker(monkeypatch, beats: _FlakyHeartbeat, until: threading.Event) -> None:
    """Start the worker with the engine stubbed out, and stop it on cue.

    ``_wait_for_shutdown`` is the seam the entrypoint exposes for exactly this —
    it is what makes an intentionally blocking process testable. Returning from
    it is what sets ``stop`` and joins the heartbeat thread, so waiting on
    ``until`` here is how the test ends the worker the moment it has seen
    enough beats.
    """
    monkeypatch.setenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", "0.01")
    monkeypatch.setattr(worker_app, "_wait_for_shutdown", lambda stop: until.wait(5))
    monkeypatch.setattr(
        "reimagina.orchestration.executor_health.heartbeat", beats, raising=True
    )
    monkeypatch.setattr("reimagina.orchestration.dbos_app.launch", lambda: None)
    worker_app.run_dbos_worker()


def test_the_loop_keeps_beating_after_a_failure(monkeypatch, caplog):
    enough = threading.Event()
    # The beat outside the loop is call 1 and is deliberately unguarded, so the
    # failures here are the second and third the *loop* makes.
    beats = _FlakyHeartbeat(fail_on={2, 3}, enough=enough, target=6)

    with caplog.at_level("WARNING"):
        _run_worker(monkeypatch, beats, until=enough)

    assert enough.is_set(), f"loop stopped after {beats.calls} beats"
    assert beats.calls >= 6
    assert any("executor heartbeat failed" in r.message for r in caplog.records), (
        "a failed beat must be logged — a silent retry hides a database that is "
        "down for good just as well as one that blipped"
    )


def test_the_first_beat_is_not_guarded(monkeypatch):
    """A worker that cannot reach its database at startup must fail loudly.

    The guard covers the loop only. Swallowing the first beat too would let a
    worker come up looking healthy against a database it has never reached.
    """
    enough = threading.Event()
    beats = _FlakyHeartbeat(fail_on={1}, enough=enough, target=1)
    with pytest.raises(RuntimeError, match="beat 1"):
        _run_worker(monkeypatch, beats, until=enough)
    assert beats.calls == 1, "the loop must not have started"
