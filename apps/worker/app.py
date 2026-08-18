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
    import os
    import threading

    from reimagina.orchestration import dbos_app
    from reimagina.orchestration.executor_health import heartbeat

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
        while not stop.wait(interval):
            heartbeat()

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
