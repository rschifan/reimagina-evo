"""The worker must be able to say what it did.

Almost everything the worker does happens where nobody is looking — that is
the point of a worker — and the reclaim sweep is the extreme case: it acts on
runs abandoned by a process that is already gone. `logger.info` is the only
channel it has.

That channel was dead. DBOS configures its own `dbos` logger with a handler,
so a worker log *looks* configured: every line in it is a DBOS line. Meanwhile
`reimagina.*` and `apps.worker` went to a root logger with no handler, at the
default WARNING level, so the sweep's startup line and every reclaim it
performed were discarded. An operator had no way to tell a fleet that
reclaims a dead peer's runs from one that never sweeps at all.

Found by running a worker and watching for a line that never came, not by
reading the code — which is why the first assertion here is the dull one
nobody writes until it has already cost them an afternoon.
"""

from __future__ import annotations

import logging
import threading

from apps.worker import app as worker_app


def _run_worker(monkeypatch, *, until: threading.Event, env: dict[str, str]):
    """Start the worker with the engine stubbed, and stop it on cue.

    `_wait_for_shutdown` is the seam the entrypoint exposes for exactly this:
    returning from it is what sets `stop` and joins the worker's threads, so
    the otherwise intentionally blocking process is testable.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(worker_app, "_wait_for_shutdown", lambda stop: until.wait(5))
    monkeypatch.setattr("reimagina.orchestration.dbos_app.launch", lambda: None)
    monkeypatch.setattr("reimagina.orchestration.dbos_app.stop_liveness", lambda: None)
    worker_app.run_dbos_worker()


def test_the_worker_configures_logging(monkeypatch):
    """Otherwise every line it writes about itself is discarded."""
    configured: list[bool] = []
    monkeypatch.setattr(
        "reimagina.logging_setup.configure_logging",
        lambda *a, **k: configured.append(True),
    )
    done = threading.Event()
    done.set()
    _run_worker(
        monkeypatch,
        until=done,
        env={"REIMAGINA_RECLAIM_ENABLED": "0"},
    )
    assert configured, (
        "the worker did not configure logging, so its own INFO lines — the "
        "sweep's interval at startup and every reclaim it performs — go to a "
        "root logger with no handler and are silently dropped"
    )


def test_the_worker_announces_its_sweep_interval(monkeypatch, caplog):
    """The only confirmation an operator gets that the sweep is switched on."""
    done = threading.Event()
    done.set()
    with caplog.at_level(logging.INFO, logger="apps.worker.app"):
        _run_worker(
            monkeypatch,
            until=done,
            env={"REIMAGINA_EXECUTOR_STALE_S": "300"},
        )
    assert any("reclaim sweep every" in r.message for r in caplog.records), (
        f"no startup line naming the sweep interval: {[r.message for r in caplog.records]}"
    )


def test_disabling_the_sweep_is_a_warning_not_a_silence(monkeypatch, caplog):
    """Turning it off is a choice with a consequence, and must be stated.

    A fleet with the sweep disabled strands a dead executor's runs forever —
    permanently `running`, with nothing to time them out. That is a defensible
    configuration and an indefensible surprise.
    """
    done = threading.Event()
    done.set()
    with caplog.at_level(logging.WARNING, logger="apps.worker.app"):
        _run_worker(
            monkeypatch,
            until=done,
            env={"REIMAGINA_RECLAIM_ENABLED": "0"},
        )
    assert any("stay in progress forever" in r.message for r in caplog.records)


def test_the_sweep_loop_actually_fires(monkeypatch, caplog):
    """The loop runs on its interval, not just at startup.

    Asserted because it could not be observed: the startup line was invisible
    for the same reason every later line was, so a sweep thread that never
    fired and one whose results were discarded looked identical from outside.
    """
    swept = threading.Event()
    calls: list[int] = []

    class _Report:
        dead_executors = ("ghost",)
        workflows_recovered = 0
        errors = ()
        acted = True

    def _fake_sweep():
        calls.append(1)
        swept.set()
        return _Report()

    monkeypatch.setattr("reimagina.orchestration.reclaim.sweep", _fake_sweep)

    with caplog.at_level(logging.INFO, logger="apps.worker.app"):
        _run_worker(
            monkeypatch,
            until=swept,
            env={
                "REIMAGINA_EXECUTOR_STALE_S": "300",
                "REIMAGINA_RECLAIM_SWEEP_S": "0.05",
            },
        )

    assert calls, "the sweep thread started but never ran a pass"
    assert any(
        "reclaim sweep: 0 workflow(s) from ghost" in r.message for r in caplog.records
    ), (
        "a sweep that acted did not say so, which is the whole failure this "
        "file exists for"
    )
