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
import contextlib
import logging
import os
import threading
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

_CLIENT = None
_CLIENT_LOCK = threading.Lock()
_PROGRESS_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("REIMAGINA_PROGRESS_SUBSCRIBERS", "32"))
)
_PROGRESS_BUFFER = 128


def _client():
    """One shared DBOS client for this process.

    ``DBOSClient`` is not a handle — constructing one builds a SQLAlchemy engine
    with its own five-connection pool and opens a connection eagerly to check
    the database. Building one per call leaked an engine and a connection every
    time, on a path polled several times a second while a run is in flight.

    Shared rather than context-managed because it is stateless for reads and
    lives as long as the process does. Never ``destroy()``ed for the same
    reason: there is no scope to tie it to.
    """
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                from dbos import DBOSClient

                from reimagina.orchestration import dbos_app

                # use_listen_notify: without it read_stream degrades to polling
                # once a second, which is the latency the SSE endpoint exists to
                # avoid.
                _CLIENT = DBOSClient(
                    system_database_url=dbos_app.dbos_dsn(), use_listen_notify=True
                )
    return _CLIENT


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
        self,
        *,
        registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
        execution_mode: str | None = None,
    ) -> None:
        self._registry = registry
        self._execution_mode = execution_mode

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
        from reimagina.orchestration import dbos_app

        dbos_app.assert_same_domain_database(conn)
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
        units: list[dbos_app.UnitInput] = [
            {
                "slug": unit.indicator,
                "features": sorted(unit.features),
                "deps": deps_of.get(unit.indicator, []),
            }
            for unit in plan.units
        ]
        params: dbos_app.RunParams = {
            "code_version": code_version,
            "scenario_id": scenario_id,
            "aoi_id": aoi_id,
        }
        dbos_app.validate_workflow_input(units, params)

        mode = self._execution_mode or dbos_app.execution_mode()
        if mode not in {"client", "executor", "dev"}:
            raise ValueError(f"unknown DBOS execution mode: {mode!r}")
        if mode == "client":
            # A client does not import or launch an executor. DBOS persists the
            # named workflow and its complete input atomically in its system
            # database; a worker with the definitions later dequeues it.
            from dbos import EnqueueOptions

            _client().enqueue(
                EnqueueOptions(
                    workflow_name=dbos_app.run_workflow.__name__,
                    queue_name=dbos_app.RUNS_QUEUE,
                    workflow_id=run_id,
                ),
                run_id,
                units,
                params,
            )
        else:
            # ``dev`` intentionally self-executes; an executor process does
            # the same when it is also an interactive submitter.
            dbos_app.launch()
            from dbos import SetWorkflowID

            with SetWorkflowID(run_id):
                dbos_app.runs_queue().enqueue(
                    dbos_app.run_workflow, run_id, units, params
                )

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
        found = _client().list_workflows(workflow_ids=[run_id])
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
        current = self.get_run(conn, run_id)
        if current is None or current.status in RUN_TERMINAL_STATES:
            return False

        # cancel_children: the units are child workflows, and leaving them
        # schedulable would let the next wave start after the run was closed.
        _client().cancel_workflow(run_id, cancel_children=True)
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
            # No envelope yet: the cancel landed inside the window between
            # `submit_run` returning and the workflow's first step writing it.
            #
            # Doing nothing here is not safe. DBOS checks for cancellation when
            # a step *begins*, so a step already past that check runs to
            # completion — and `s_open_run` would then create the envelope as
            # `running`, permanently, for a run that was cancelled. Writing the
            # envelope ourselves, already terminal, closes that: `create_run` is
            # ON CONFLICT DO NOTHING so the workflow's later attempt is a no-op,
            # and `finalize_run` refuses to move a terminal run.
            runstore.create_run(
                conn,
                run_id=run_id,
                scenario_id=None,
                aoi_id=None,
                code_version="cancelled-before-start",
                status=final,
            )
            logger.info("cancelled run %s before its envelope was written", run_id)
        conn.commit()
        return True

    # ── Subscribe ───────────────────────────────────────────────────────────

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Stream progress from the workflow's own durable stream.

        ``DBOSClient`` rather than the ambient ``DBOS``, so a reader process
        need not host the workflow definitions — the API can follow a run a
        worker is executing.

        ``read_stream`` is a *synchronous* generator that blocks until the
        stream closes, so it runs on a thread and hands events back through a
        queue.

        The thread is **dedicated and daemonised, not borrowed from the default
        executor**. A future returned by ``run_in_executor`` cannot be
        cancelled once its callable is running, and this callable blocks for as
        long as the run does — so an SSE client that disconnects one second into
        a twenty-minute run used to leave a thread pinned for the remaining
        nineteen, pushing into a queue nobody drained. The default executor caps
        at ``min(32, cpu+4)`` and is shared with every ``asyncio.to_thread`` in
        the process, so roughly thirty such disconnects would deadlock the API's
        entire thread-offload path.

        It still cannot be interrupted — that is a property of the underlying
        generator, not of how it is scheduled. A process-wide slot limit bounds
        the remaining daemon threads, and the bounded handoff queue turns a
        slow SSE consumer into a reconnect rather than unbounded memory growth.
        """
        import threading as _threading

        from reimagina.orchestration import dbos_app

        # A snapshot first, so a subscriber that arrives after the run finished
        # is told the outcome instead of waiting for events that will never come.
        snapshot = self._snapshot(run_id)
        if snapshot is not None:
            yield snapshot
            if snapshot.status in RUN_TERMINAL_STATES:
                return

        if not _PROGRESS_SLOTS.acquire(blocking=False):
            raise RuntimeError(
                "progress subscriber capacity reached; reconnect shortly"
            )

        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue(maxsize=_PROGRESS_BUFFER)
        done = object()
        stop = _threading.Event()

        def _offer(payload) -> None:
            """Run in the event loop; keep the producer bounded on overflow."""
            if events.full():
                logger.warning("progress subscriber for %s fell behind", run_id)
                stop.set()
                with contextlib.suppress(asyncio.QueueEmpty):
                    events.get_nowait()
                events.put_nowait(done)
                return
            events.put_nowait(payload)

        def _pump() -> None:
            try:
                for payload in _client().read_stream(run_id, dbos_app.PROGRESS_STREAM):
                    if stop.is_set():
                        return
                    # The loop may already be closed if the process is shutting
                    # down; there is no subscriber left to care either way.
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(_offer, payload)
            except Exception as exc:  # noqa: BLE001 — surfaced as stream end
                logger.warning("progress stream for %s ended: %s", run_id, exc)
            finally:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_offer, done)

        thread = _threading.Thread(
            target=_pump, name=f"dbos-progress-{run_id[:8]}", daemon=True
        )
        thread.start()
        try:
            while True:
                payload = await events.get()
                if payload is done:
                    return
                yield RunEvent(
                    run_id=payload.get("run_id", run_id),
                    indicator=payload.get("indicator"),
                    status=payload.get("status", ""),
                    detail=payload.get("detail"),
                )
        finally:
            # Tells the pump to stop at its next event. It cannot be woken any
            # sooner, so the thread outlives this generator until the stream
            # produces something or closes.
            stop.set()
            _PROGRESS_SLOTS.release()

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
