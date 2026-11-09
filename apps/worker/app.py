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
    by executor id and DBOS defaults every process to ``local``, so sharing it
    would make each process claim the others' in-flight work.
    """
    import logging
    import os
    import threading

    from reimagina.orchestration import dbos_app
    from reimagina.orchestration.executor_health import interval_seconds
    from reimagina.orchestration.reclaim import stale_after_seconds, sweep

    logger = logging.getLogger(__name__)

    # Distinguishes this process from the API on the same host, so each
    # reclaims only its own interrupted work. Explicit REIMAGINA_EXECUTOR_ID
    # still wins.
    os.environ.setdefault("REIMAGINA_EXECUTOR_ROLE", "worker")
    os.environ.setdefault("REIMAGINA_DBOS_EXECUTION_MODE", "executor")

    # Validated before launch, not after: both raise on a misconfiguration,
    # and failing with the queues already draining would leave work half-done
    # in a process about to exit.
    beat_interval = interval_seconds()
    stale_after = stale_after_seconds()

    # Launching starts the liveness heartbeat for this executor.
    dbos_app.launch()

    stop = threading.Event()
    sweep_every = float(
        os.environ.get("REIMAGINA_RECLAIM_SWEEP_S", max(stale_after / 2, 60.0))
    )
    enabled = os.environ.get("REIMAGINA_RECLAIM_ENABLED", "1") != "0"

    def _sweep_loop() -> None:
        """Reclaim dead executors' workflows until asked to stop.

        The first sweep waits a full interval rather than running at startup.
        A process that has just launched has not yet beaten often enough for
        its *peers* to look stale relative to it, and more importantly a fleet
        restarting together would otherwise have every member sweep every
        other member in the same second.
        """
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
