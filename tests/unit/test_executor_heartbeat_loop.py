"""A failed beat must not end the reporter.

``heartbeat()`` opens a short direct connection, so a failover, a restart or a
momentarily full pool raises inside the loop thread. Unhandled, that ends the
thread while the process keeps dequeuing and executing units — the row in
``jobs_runtime.ops_executor_heartbeat`` simply stops advancing, with nothing
logged. "Stale but very much alive" is the one input the cross-executor sweep's
liveness gate must never be handed, and under DBOS 3 that gate is the only
thing standing between a slow peer and having its running work re-enqueued
alongside it.

Driven through the real ``start_heartbeat`` with a real thread rather than by
calling the closure directly: the loop, its interval, its ``stop`` event and
its error handling only exist together, and a test that reached inside would
pass against a version where they had come apart.

⚠ These used to drive ``apps.worker.app.run_dbos_worker``, which owned the
loop. It moved to ``executor_health`` when liveness became every executor's
job rather than the worker's — the API launches an executor too, and an
executor that does not announce itself cannot be swept. The properties
asserted here did not change; only the unit that owns them did.
"""

from __future__ import annotations

import threading

import pytest

from reimagina.orchestration import dbos_app, executor_health


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


def test_the_loop_keeps_beating_after_a_failure(monkeypatch, caplog):
    enough = threading.Event()
    # The beat outside the loop is call 1 and is deliberately unguarded, so the
    # failures here are the second and third the *loop* makes.
    beats = _FlakyHeartbeat(fail_on={2, 3}, enough=enough, target=6)
    monkeypatch.setattr(executor_health, "heartbeat", beats)
    stop = threading.Event()

    with caplog.at_level("WARNING"):
        thread = executor_health.start_heartbeat(stop, interval=0.01)
        enough.wait(5)
        stop.set()
        thread.join(timeout=2)

    assert enough.is_set(), f"loop stopped after {beats.calls} beats"
    assert beats.calls >= 6
    assert any("executor heartbeat failed" in r.message for r in caplog.records), (
        "a failed beat must be logged — a silent retry hides a database that is "
        "down for good just as well as one that blipped"
    )


def test_the_first_beat_is_not_guarded(monkeypatch):
    """A process that cannot record its liveness must fail loudly.

    The guard covers the loop only. Swallowing the first beat too would let a
    process come up executing units while invisible to the sweep, so its runs
    strand permanently if it dies.
    """
    enough = threading.Event()
    beats = _FlakyHeartbeat(fail_on={1}, enough=enough, target=1)
    monkeypatch.setattr(executor_health, "heartbeat", beats)

    with pytest.raises(RuntimeError, match="beat 1"):
        executor_health.start_heartbeat(threading.Event(), interval=0.01)
    assert beats.calls == 1, "the loop must not have started"


def test_a_non_positive_interval_is_refused(monkeypatch):
    """Zero would spin; negative is nonsense. Both must say so at startup."""
    monkeypatch.setenv("REIMAGINA_EXECUTOR_HEARTBEAT_S", "0")
    with pytest.raises(RuntimeError, match="must be positive"):
        executor_health.interval_seconds()


def test_launching_an_executor_starts_its_liveness(monkeypatch):
    """Liveness belongs to `launch`, not to the worker entrypoint.

    Any process that launches registers the queues with a worker concurrency
    and can therefore dequeue and execute units — the API included. If only
    the worker announced itself, an API process that died mid-unit would
    strand that run forever.
    """
    started: list[threading.Event] = []

    def _fake_start(stop, *, interval=None):
        started.append(stop)
        return threading.Thread(target=lambda: None)

    monkeypatch.setattr(executor_health, "start_heartbeat", _fake_start)
    monkeypatch.setattr(dbos_app, "_heartbeat_thread", None)
    dbos_app._start_liveness()
    try:
        assert started, "launch() did not start the executor heartbeat"
    finally:
        monkeypatch.setattr(dbos_app, "_heartbeat_thread", None)
