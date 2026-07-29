"""Worker application — drains ops-plane work. Requires ``DATABASE_URL_DIRECT``.

This branch runs the Hatchet ops-plane. The worker registers the indicator DAG
with the engine and executes the tasks the engine assigns it; it does not
schedule, poll a queue, or decide what is ready. All of that is the engine's.

Two consequences worth stating, because they are the comparison:

- A worker is **mandatory** here, not an optimisation. Nothing executes without
  one, and — measured, spike B0 — a submitted run is not even *observable*
  until a worker has registered the workflow definition: ``runs.get()`` returns
  404 on a run id the engine itself just minted. The library track has no
  equivalent state; there the submitting process can drive and answer for its
  own run.
- There is no recovery story to write. A worker that dies mid-task releases its
  slot and the engine reassigns the work; nothing here is scoped by an executor
  id, because run ownership is the engine's to track. Track A spends a runbook
  section on exactly this.
"""

from __future__ import annotations

import os
from contextlib import closing


def create_worker() -> dict[str, str]:
    """Scaffold descriptor (kept for the boot smoke-test / health surface)."""
    return {"app": "worker", "namespace": "reimagina", "status": "ready"}


def run_hatchet_worker(
    *, name: str | None = None, slots: int | None = None, blocking: bool = True
):
    """Register the DAG and start executing. Blocks unless ``blocking=False``.

    ``slots`` is how many units this process runs at once. Each in-flight unit
    holds its own psycopg2 connection for the whole of a multi-minute PostGIS
    statement, so this is a database-connection budget as much as a CPU one.

    ``blocking=False`` returns the unstarted worker instead of running it, so a
    test fixture can drive one in-process and shut it down afterwards. Nothing
    in production should use it.
    """
    if not os.environ.get("DATABASE_URL_DIRECT"):
        # Fail here rather than inside a unit. Otherwise the engine schedules
        # work, hands it to this process, and every task fails minutes later
        # with a KeyError from deep inside a runner.
        raise RuntimeError(
            "DATABASE_URL_DIRECT is required — unit runners open their own "
            "connection to the domain database, which is not the engine's."
        )
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        raise RuntimeError(
            "HATCHET_CLIENT_TOKEN is required — the worker cannot register "
            "workflows or receive Hatchet tasks without it."
        )

    resolved_slots = (
        slots
        if slots is not None
        else int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "2"))
    )
    if resolved_slots < 1:
        raise ValueError("worker slots must be at least 1")

    from reimagina.orchestration import hatchet_app
    from reimagina.orchestration.database_identity import assert_same_domain_database
    from reimagina.db import connect

    # Validate the target is migrated before the worker advertises a workflow.
    with closing(connect(os.environ["DATABASE_URL_DIRECT"])) as conn:
        assert_same_domain_database(conn, os.environ["DATABASE_URL_DIRECT"])

    worker = hatchet_app.get_hatchet().worker(
        name or os.environ.get("REIMAGINA_WORKER_NAME", "reimagina-worker"),
        slots=resolved_slots,
        workflows=[hatchet_app.get_run_workflow(), hatchet_app.get_outbox_workflow()],
    )
    if not blocking:
        return worker
    worker.start()
    return worker
