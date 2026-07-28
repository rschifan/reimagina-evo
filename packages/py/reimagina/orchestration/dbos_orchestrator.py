"""The Orchestrator port over DBOS.

Submits a durable workflow and returns; the caller polls ``get_run`` or streams
``subscribe_progress``. Run state stays in ``jobs_runtime`` — this track keeps
its own status vocabulary rather than adopting an engine's.

The run id *is* the DBOS workflow id. That is not a convenience: it makes a run
addressable from any process without a mapping table, so the API can read a
progress stream or cancel a run that a worker is executing, and it makes submit
idempotent — re-submitting the same id attaches to the existing workflow rather
than starting a second one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import runstore
from reimagina.orchestration.plan import noop_handle, plan_run
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    UNIT_TERMINAL_STATES,
    RunEvent,
    RunHandle,
    RunStatus,
    UnitStatusView,
    overall_status,
)
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)

# DBOS's workflow-status enum mapped onto ours, for the window before the run
# envelope exists. Only ever consulted when jobs_runtime has nothing yet, so a
# real run status always wins over this.
_RUN_STATUS_FOR = {
    "ENQUEUED": "queued",
    "DELAYED": "queued",
    "PENDING": "running",
    "SUCCESS": "succeeded",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "failed",
}


class DBOSOrchestrator:
    """Durable execution with no standing service."""

    def __init__(
        self, *, registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS
    ) -> None:
        self._registry = registry

    # ── Submit ──────────────────────────────────────────────────────────────

    def submit_run(
        self,
        conn: object,
        *,
        changed_attributes: Iterable[str],
        edited_features: Iterable[str],
        scenario_id: str | None = None,
        aoi_id: str | None = None,
        code_version: str = "dev",
    ) -> RunHandle:
        from dbos import DBOS, SetWorkflowID

        from reimagina.orchestration import dbos_app

        dbos_app.launch()
        run_id = str(uuid4())

        plan = plan_run(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            registry=self._registry,
        )
        if plan.is_noop:
            return noop_handle(run_id)

        # The dirty set is derived HERE and handed to the workflow as an input,
        # rather than recomputed inside it. A workflow that rebuilt its own unit
        # list from the catalog would, on replay after a catalog edit, hand one
        # indicator's checkpointed result to another — silently. See spike 2 in
        # docs/product/PHASE3_OPS_PLANE_DBOS_SPIKES.md.
        deps_of = {
            spec.slug: [dep for dep, _weight in spec.depends_on]
            for spec in self._registry
        }
        units = [
            {
                "slug": unit.indicator,
                "features": sorted(unit.features),
                "deps": deps_of.get(unit.indicator, []),
            }
            for unit in plan.units
        ]

        with SetWorkflowID(run_id):
            DBOS.start_workflow(dbos_app.run_workflow, run_id, units, plan.params)

        return RunHandle(run_id=run_id, status=None)

    # ── Read ────────────────────────────────────────────────────────────────

    def get_run(self, conn: object, run_id: str) -> RunStatus | None:
        """Detail from our tables; *existence* from the engine.

        A submitted run is durable the moment ``start_workflow`` returns, but
        its envelope is written by the workflow's own first step — so there is a
        window where the run exists and ``jobs_runtime`` has nothing to say
        about it. Reporting ``None`` there would break the port's contract that
        a submitted id is immediately retrievable.

        Writing the envelope in ``submit_run`` instead would close that window
        and open a worse one: a crash between the commit and the enqueue leaves
        a run marked ``running`` that nothing will ever drive. That is exactly
        the wedge this project already documented in the queue-based ops-plane.
        Making the engine the existence oracle avoids both.
        """
        row = runstore.get_run(conn, run_id)
        if row is not None:
            return RunStatus(
                run_id=row["run_id"],
                status=row["status"],
                units=tuple(
                    UnitStatusView(u["indicator"], u["status"], u["detail"])
                    for u in row["units"]
                ),
            )
        return self._from_workflow(run_id)

    def _from_workflow(self, run_id: str) -> RunStatus | None:
        """Reconstruct a run from the engine alone, for the pre-envelope window.

        The unit list needs no reconstructing: it *is* the workflow's input, and
        DBOS persisted it as part of making the run durable. So a run is fully
        describable from the moment it is submitted, without our tables having
        been touched — which is what lets the envelope be written by the
        workflow rather than by the submitter.
        """
        from dbos import DBOSClient

        from reimagina.orchestration import dbos_app

        client = DBOSClient(system_database_url=dbos_app.dbos_dsn())
        found = client.list_workflows(workflow_ids=[run_id])
        if not found:
            return None
        args = (found[0].input or {}).get("args", ())
        units = args[1] if len(args) > 1 else []
        return RunStatus(
            run_id=run_id,
            status=_RUN_STATUS_FOR.get(found[0].status, "running"),
            units=tuple(
                UnitStatusView(u["slug"], "queued", None)
                for u in units
                if isinstance(u, dict) and "slug" in u
            ),
        )

    # ── Cancel ──────────────────────────────────────────────────────────────

    def cancel_run(self, conn: object, run_id: str) -> bool:
        """Stop scheduling, stop the running statement, then close the run.

        Three mechanisms, because each stops something the others cannot.
        ``DBOS.cancel_workflow`` prevents the workflow and its children from
        being scheduled further, but a step already inside a blocking psycopg
        call keeps going — DBOS observes cancellation only at the next step
        boundary. ``pg_cancel_backend`` is what actually interrupts that
        statement (r031). Marking the units closes the run in our own vocabulary.
        """
        from dbos import DBOS

        from reimagina.orchestration import dbos_app

        current = self.get_run(conn, run_id)
        if current is None or current.status in RUN_TERMINAL_STATES:
            return False

        dbos_app.launch()
        # cancel_children: the units are child workflows, and leaving them
        # schedulable would let the next wave start after the run was closed.
        DBOS.cancel_workflow(run_id, cancel_children=True)
        backend_for_connection(conn).job_run_repo().cancel_run_backends(run_id)

        for unit in current.units:
            if unit.status in UNIT_TERMINAL_STATES:
                continue
            runstore.set_unit_status(
                conn,
                run_id=run_id,
                indicator_slug=unit.indicator,
                status="cancelled",
                detail="run cancelled",
            )

        # Re-read rather than assuming the writes landed: a unit can reach a
        # terminal state between the snapshot above and the update, and
        # set_unit_status refuses to move it back.
        units = runstore.list_units(conn, run_id)
        final = overall_status(u["status"] for u in units) if units else "cancelled"
        if not runstore.finalize_run(conn, run_id=run_id, status=final):
            # No envelope yet — the workflow was cancelled before its first
            # step. The engine's own CANCELLED status is the whole record, and
            # get_run reports it.
            logger.info("cancelled run %s before its envelope was written", run_id)
        conn.commit()
        return True

    # ── Subscribe ───────────────────────────────────────────────────────────

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Stream progress from the workflow's own durable stream.

        ``DBOSClient`` rather than the ambient ``DBOS``, so a reader process
        need not host the workflow definitions — the API can follow a run a
        worker is executing.

        ``read_stream`` is a *synchronous* generator that blocks on
        LISTEN/NOTIFY, so it runs on a worker thread and hands events back
        through a queue. Pulling it with ``asyncio.to_thread`` per item would
        leave a thread blocked inside the generator when the subscriber
        disconnects, still holding the connection this coroutine is about to
        close.
        """
        from dbos import DBOSClient

        from reimagina.orchestration import dbos_app

        client = DBOSClient(system_database_url=dbos_app.dbos_dsn())
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        _DONE = object()

        def _pump() -> None:
            try:
                for payload in client.read_stream(run_id, dbos_app.PROGRESS_STREAM):
                    loop.call_soon_threadsafe(events.put_nowait, payload)
            except Exception as exc:  # noqa: BLE001 — surfaced as stream end
                logger.warning("progress stream for %s ended: %s", run_id, exc)
            finally:
                loop.call_soon_threadsafe(events.put_nowait, _DONE)

        # A snapshot first, so a subscriber that arrives after the run finished
        # is told the outcome instead of waiting for events that will never come.
        snapshot = self._snapshot(run_id)
        if snapshot is not None:
            yield snapshot
            if snapshot.status in RUN_TERMINAL_STATES:
                return

        task = loop.run_in_executor(None, _pump)
        try:
            while True:
                payload = await events.get()
                if payload is _DONE:
                    return
                yield RunEvent(
                    run_id=payload.get("run_id", run_id),
                    indicator=payload.get("indicator"),
                    status=payload.get("status", ""),
                    detail=payload.get("detail"),
                )
        finally:
            task.cancel()

    def _snapshot(self, run_id: str) -> RunEvent | None:
        """The run-level status, read without the caller's connection.

        The port hands ``subscribe_progress`` no connection — no engine's
        progress transport is the caller's pooled one — so this opens its own.
        """
        from reimagina.db import connect

        from reimagina.orchestration import dbos_app

        with connect(dbos_app.dbos_dsn()) as conn:
            row = runstore.get_run(conn, run_id)
        return RunEvent(
            run_id=run_id,
            indicator=None,
            status=row["status"] if row else "unknown",
        )
