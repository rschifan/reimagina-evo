"""Worker application — drains ops-plane work. Requires ``DATABASE_URL_DIRECT``.

This branch runs the DBOS ops-plane. A DBOS worker is not a queue consumer with
a poll loop of its own: launching registers the workflow definitions, starts the
queue dequeuer and the startup-recovery thread, and then there is nothing to do
but stay alive. All the interesting behaviour is in the library.

Two consequences worth stating, because they are the comparison:

- A worker is **optional** here. DBOS is a library, so the process that submits
  a run can also drain it; this exists to scale out, not to make runs possible.
- Launching reclaims *this executor's* interrupted workflows automatically. It
  does not touch another executor's — that needs a deliberate sweep against a
  liveness signal, which is not built yet (see the spike report).

"""

from __future__ import annotations


def create_worker() -> dict[str, str]:
    """Scaffold descriptor (kept for the boot smoke-test / health surface)."""
    return {"app": "worker", "namespace": "reimagina", "status": "ready"}


def run_dbos_worker() -> None:
    """Launch DBOS and block. Recovery of this executor's own work is automatic.

    ``REIMAGINA_EXECUTOR_ID`` must be distinct per process: recovery is scoped
    by executor id and DBOS defaults every process to ``local``, so sharing it
    would make each process claim the others' in-flight work.
    """
    import threading

    from reimagina.orchestration import dbos_app

    dbos_app.launch()
    threading.Event().wait()  # nothing to poll; the library owns the loops
