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

import pytest

from apps.worker import app as worker_app
from reimagina.orchestration import reclaim


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

    # The real dataclass, not a stand-in with the fields this test happens to
    # read. A hand-rolled stub went stale the moment `ReclaimReport` grew a
    # field the worker logs, and the symptom was an AttributeError raised on a
    # daemon thread — which pytest reports as a warning, so the suite stayed
    # green while the loop under test died on its first pass.
    def _fake_sweep():
        calls.append(1)
        swept.set()
        return reclaim.ReclaimReport(dead_executors=("ghost",), workflows_recovered=0)

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


def test_a_stranded_executor_is_reported_on_the_sweep_line(monkeypatch, caplog):
    """Keeping a liveness row is a decision, so it has to be audible.

    ``reclaim.forget_executor`` declines to forget an executor that still has
    unfinished workflows — almost always because recovery filters on
    ``application_version`` and this process is on a different one. Silent,
    the fleet logs look like a healthy sweep while work leaks.
    """
    swept = threading.Event()

    def _fake_sweep():
        swept.set()
        return reclaim.ReclaimReport(
            dead_executors=("ghost",),
            workflows_recovered=0,
            stranded_executors=("ghost",),
        )

    monkeypatch.setattr("reimagina.orchestration.reclaim.sweep", _fake_sweep)
    with caplog.at_level(logging.WARNING, logger="apps.worker.app"):
        _run_worker(
            monkeypatch,
            until=swept,
            env={
                "REIMAGINA_EXECUTOR_STALE_S": "300",
                "REIMAGINA_RECLAIM_SWEEP_S": "0.05",
            },
        )
    assert any("left ghost unreclaimed" in r.message for r in caplog.records), (
        f"a strand was not reported: {[r.message for r in caplog.records]}"
    )


class TestSweepInterval:
    """``REIMAGINA_RECLAIM_SWEEP_S`` is rejected, not coerced, when unusable.

    Every unusable value fails as something else. ``0`` or a negative number
    makes ``Event.wait(timeout)`` return immediately, so the loop becomes a
    busy spin — a full sweep, connecting and querying, as fast as Postgres
    will answer, forever, on a daemon thread nobody watches. The symptom is a
    worker that looks idle while holding a connection permanently busy. A
    non-numeric value raised a bare ``ValueError`` from ``float()`` at
    startup, naming neither the variable nor the value.
    """

    def test_the_default_is_half_the_threshold_floored_at_a_minute(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("REIMAGINA_RECLAIM_SWEEP_S", raising=False)
        assert worker_app._sweep_interval(600.0) == 300.0
        assert worker_app._sweep_interval(300.0) == 150.0
        assert worker_app._sweep_interval(60.0) == 60.0  # the floor, not 30

    def test_an_explicit_value_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("REIMAGINA_RECLAIM_SWEEP_S", "12.5")
        assert worker_app._sweep_interval(300.0) == 12.5

    def test_an_empty_value_falls_back_to_the_default(self, monkeypatch) -> None:
        """An unset variable in a `.env` file reads as empty, not absent."""
        monkeypatch.setenv("REIMAGINA_RECLAIM_SWEEP_S", "")
        assert worker_app._sweep_interval(300.0) == 150.0

    @pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "soon", "30s"])
    def test_an_unusable_value_is_refused_by_name(self, monkeypatch, value) -> None:
        monkeypatch.setenv("REIMAGINA_RECLAIM_SWEEP_S", value)
        with pytest.raises(RuntimeError, match="REIMAGINA_RECLAIM_SWEEP_S"):
            worker_app._sweep_interval(300.0)


def test_disabling_the_sweep_skips_its_configuration_entirely(monkeypatch, caplog):
    """`REIMAGINA_RECLAIM_ENABLED=0` must get a worker up, not argue with it.

    The threshold and the sweep period are the sweep's own configuration, and
    they were validated before the flag was read — so a worker explicitly
    told not to sweep still refused to boot on a bad
    `REIMAGINA_EXECUTOR_STALE_S`. That is the one setting the operator has
    just stopped using, and turning the sweep off is the documented way out of
    a reclaim misconfiguration.
    """
    done = threading.Event()
    done.set()
    with caplog.at_level(logging.WARNING, logger="apps.worker.app"):
        _run_worker(
            monkeypatch,
            until=done,
            env={
                "REIMAGINA_RECLAIM_ENABLED": "0",
                # Both would raise if they were still read.
                "REIMAGINA_EXECUTOR_STALE_S": "1",
                "REIMAGINA_RECLAIM_SWEEP_S": "not-a-number",
            },
        )
    assert any("stay in progress forever" in r.message for r in caplog.records)
