"""Worker application — drains ops-plane work. Requires ``DATABASE_URL_DIRECT``.

This branch runs the DBOS ops-plane. A DBOS worker is not a queue consumer with
a poll loop of its own: launching registers the workflow definitions, starts the
queue dequeuer and the startup-recovery thread, and then there is nothing to do
but stay alive. All the interesting behaviour is in the library.

Two kinds of recovery meet here, and they are not the same mechanism:

- **Its own.** ``dbos_app.launch()`` reclaims the workflows recorded against
  *this* ``executor_id``. That covers a restart, and needs nothing from this
  module.
- **Someone else's.** An executor that never restarts — a destroyed host, a
  scaled-away replica — leaves PENDING workflows nobody will claim. The sweep
  below reclaims them, gated on the liveness table so a merely slow peer is
  never mistaken for a dead one. ``orchestration.reclaim`` holds the argument
  for why the gate is a correctness requirement rather than a courtesy.

The sweep lives in the worker rather than in the submit path because it wants
a long-lived process with an executor to recover *onto*, and because a
submit-time sweep would fire at the one moment the system is already busy.
"""

from __future__ import annotations


def create_worker() -> dict[str, str]:
    """Scaffold descriptor (kept for the boot smoke-test / health surface)."""
    return {"app": "worker", "namespace": "reimagina", "status": "ready"}


def _wait_for_shutdown(stop) -> None:
    """Tiny seam for the otherwise intentionally blocking worker entrypoint."""
    stop.wait()


def run_dbos_worker() -> None:
    """Launch DBOS and block. Recovery of this executor's own work is automatic.

    ``REIMAGINA_EXECUTOR_ID`` must be distinct per process: recovery is scoped
    by executor id, and the default ``<hostname>:worker`` is shared by every
    worker on one host, so two there would claim each other's in-flight work.
    """
    import logging
    import os
    import threading

    from reimagina.logging_setup import configure_logging
    from reimagina.orchestration import dbos_app
    from reimagina.orchestration.executor_health import interval_seconds
    from reimagina.orchestration.reclaim import (
        stale_after_seconds,
        sweep,
        sweep_interval,
    )

    # ⚠ Without this the worker emits nothing of its own. DBOS configures its
    # own `dbos` logger with a handler, so a worker log *looks* configured —
    # every line in it is a DBOS line — while `reimagina.*` and this module go
    # to a root logger with no handler and the default WARNING level.
    #
    # That is worse here than almost anywhere else. The sweep acts on work
    # nobody is watching, by design, and reports what it reclaimed through
    # `logger.info`. Silent, an operator has no way to tell a fleet that
    # reclaims a dead peer's runs from one that never sweeps at all — and the
    # startup line naming the interval, which is the only confirmation the
    # feature is switched on, disappears too.
    #
    # The CLI has called this since it was written. The worker never did.
    configure_logging()
    logger = logging.getLogger(__name__)

    # Distinguishes this process from the API on the same host, so each
    # reclaims only its own interrupted work. Explicit REIMAGINA_EXECUTOR_ID
    # still wins.
    os.environ.setdefault("REIMAGINA_EXECUTOR_ROLE", "worker")
    os.environ.setdefault("REIMAGINA_DBOS_EXECUTION_MODE", "executor")

    enabled = os.environ.get("REIMAGINA_RECLAIM_ENABLED", "1") != "0"

    # Validated before launch, not after: these raise on a misconfiguration,
    # and failing with the queues already draining would leave work half-done
    # in a process about to exit.
    #
    # The heartbeat interval is always validated — every executor beats,
    # whether or not it sweeps. The staleness threshold and the sweep period
    # are the sweep's own configuration, so they are validated only when the
    # sweep is on. Otherwise `REIMAGINA_RECLAIM_ENABLED=0` — the documented
    # way to turn the sweep off, including to get a worker up *because*
    # something about reclaim is misconfigured — would still refuse to boot on
    # a bad `REIMAGINA_EXECUTOR_STALE_S`, which is the one setting the operator
    # has just stopped using.
    beat_interval = interval_seconds()
    stale_after = stale_after_seconds() if enabled else None
    sweep_every = sweep_interval(stale_after) if stale_after is not None else None

    # Launching starts the liveness heartbeat for this executor.
    dbos_app.launch()

    stop = threading.Event()

    def _sweep_loop() -> None:
        """Reclaim dead executors' workflows until asked to stop.

        The first sweep waits a full interval rather than running at startup.
        A process that has just launched has not yet beaten often enough for
        its *peers* to look stale relative to it, and more importantly a fleet
        restarting together would otherwise have every member sweep every
        other member in the same second.
        """
        assert sweep_every is not None  # only started when the sweep is enabled
        while not stop.wait(sweep_every):
            try:
                report = sweep()
            except Exception:  # noqa: BLE001 — a failed sweep must not end the loop
                logger.warning("reclaim sweep failed; will retry", exc_info=True)
                continue
            if report.acted:
                logger.info(
                    "reclaim sweep: %d workflow(s) from %s",
                    report.workflows_recovered,
                    ", ".join(report.dead_executors),
                )
            if report.stranded_executors:
                # Kept deliberately, not failed: reclaim.forget_executor has
                # already explained why at WARNING. Repeated here so the line
                # an operator greps for ("reclaim sweep") carries it too.
                logger.warning(
                    "reclaim sweep left %s unreclaimed — work remains open "
                    "against them that this application version cannot recover",
                    ", ".join(report.stranded_executors),
                )
            for error in report.errors:
                logger.warning("reclaim sweep could not finish an executor: %s", error)

    threads = []
    if enabled:
        sweeper = threading.Thread(
            target=_sweep_loop, name="dbos-reclaim-sweep", daemon=True
        )
        sweeper.start()
        threads.append(sweeper)
        logger.info(
            "reclaim sweep every %gs for executors silent for %gs (beat %gs)",
            sweep_every,
            stale_after,
            beat_interval,
        )
    else:
        logger.warning(
            "REIMAGINA_RECLAIM_ENABLED=0 — a dead executor's runs will stay "
            "in progress forever until a process with its id restarts"
        )

    try:
        _wait_for_shutdown(stop)  # DBOS owns the dequeue/recovery loops.
    finally:
        stop.set()
        dbos_app.stop_liveness()
        for thread in threads:
            thread.join(timeout=5.0)
