"""Worker application — drains ops-plane work. Requires ``DATABASE_URL_DIRECT``.

This branch runs the DBOS ops-plane. A DBOS worker is not a queue consumer with
a poll loop of its own: launching registers the workflow definitions, starts the
queue dequeuer and the startup-recovery thread, and then there is nothing to do
but stay alive. All the interesting behaviour is in the library.

Launching reclaims *this executor's* interrupted workflows automatically. It
  does not touch another executor's — that needs a deliberate sweep against a
  liveness signal, which is not built yet (see the spike report).

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
    from reimagina.orchestration.executor_health import heartbeat

    logger = logging.getLogger(__name__)

    # Distinguishes this process from the API on the same host, so each
    # reclaims only its own interrupted work. Explicit REIMAGINA_EXECUTOR_ID
    # still wins.
    os.environ.setdefault("REIMAGINA_EXECUTOR_ROLE", "worker")
    os.environ.setdefault("REIMAGINA_DBOS_EXECUTION_MODE", "executor")
    dbos_app.launch()
    stop = threading.Event()
    interval = float(os.environ.get("REIMAGINA_EXECUTOR_HEARTBEAT_S", "30"))
    if interval <= 0:
        raise RuntimeError("REIMAGINA_EXECUTOR_HEARTBEAT_S must be positive")

    def _heartbeat_loop() -> None:
        """Publish liveness until asked to stop. Survives a failed beat.

        ⚠ The ``except`` is the point, and it is not defensive padding.
        ``heartbeat()`` opens a short direct connection, so any ordinary blip —
        a failover, a restart, a full connection pool, a network hiccup — raises
        here. Unhandled, that ends the *thread*, silently: the worker keeps
        dequeuing and executing units while its row in
        ``jobs_runtime.ops_executor_heartbeat`` stops advancing forever, and
        nothing logs a word.

        Which is worse than a dead thread, because of what reads that row.
        ``dbos>=2.28,<3`` is pinned below the idempotent-recovery fix, so the
        cross-executor sweep is deliberately gated on a conservative liveness
        signal — and "stale but very much alive" is the one input that gate must
        never be handed. A missed beat is transient; a dead reporter is not.
        """
        while not stop.wait(interval):
            try:
                heartbeat()
            except Exception:  # noqa: BLE001 — a missed beat must not end the loop
                logger.warning(
                    "executor heartbeat failed; retrying in %ss",
                    interval,
                    exc_info=True,
                )

    # Not guarded: the first beat runs before the loop starts, and a worker that
    # cannot reach its own database at startup should fail loudly rather than
    # come up looking healthy.
    heartbeat()
    thread = threading.Thread(
        target=_heartbeat_loop, name="dbos-executor-heartbeat", daemon=True
    )
    thread.start()
    try:
        _wait_for_shutdown(stop)  # DBOS owns the dequeue/recovery loops.
    finally:
        stop.set()
        thread.join(timeout=interval + 1)
