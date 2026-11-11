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
from contextlib import closing as _closing
from reimagina.orchestration.port import (
    DBConnection,
    RUN_TERMINAL_STATES,
    UNIT_TERMINAL_STATES,
    OpsPlaneNotProvisioned,
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


def _persist_plan(units) -> str:
    """Store the resolved feature set; return the handle the workflow carries.

    ⚠ Commits and closes before returning. A worker can dequeue the moment the
    workflow is enqueued and reads the plan on its own connection, so an
    uncommitted write is indistinguishable from a lost one. Owning the
    transaction here makes "commit before enqueue" structural rather than an
    ordering someone has to remember.

    ⚠ Its own connection, not ``submit_run``'s. That one is borrowed from the
    caller and committing it would end a transaction this layer does not own —
    the same reason ``record_failed_job_run`` opens its own.

    ⚠ Not in ``orchestration/plan.py``, which states it "touches no queue,
    worker or run table"; this writes to ``jobs_runtime``.
    """
    from uuid import uuid4

    from reimagina.db import connect
    from reimagina.storage import backend_for_connection as _backend

    from reimagina.orchestration import dbos_app

    plan_id = str(uuid4())
    with _closing(connect(dbos_app.dbos_dsn())) as plan_conn:
        _backend(plan_conn).run_plan_repo().write(
            plan_id=plan_id,
            units={u.indicator: sorted(u.features) for u in units},
        )
        # Opportunistic orphan reclaim, on the connection already open. The
        # table holds three rows per in-flight run and nothing else, so this is
        # a scan of tens of rows — cheap enough to do here and worth more than a
        # cron nobody remembers to schedule. TTL is generous by default: see
        # `RunPlanRepository.sweep`.
        _backend(plan_conn).run_plan_repo().sweep(
            older_than_hours=int(os.environ.get("REIMAGINA_RUN_PLAN_TTL_HOURS", "24"))
        )
        plan_conn.commit()
    return plan_id


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
        conn: DBConnection,
        *,
        changed_attributes: Iterable[str],
        edited_features: Iterable[str],
        scenario_id: str | None = None,
        aoi_id: str | None = None,
        code_version: str = "dev",
        indicators: Sequence[str] | None = None,
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
            indicators=indicators,
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
                # ⚠ Empty, deliberately. The ids live in `jobs_runtime.run_plan`
                # under `params["plan_id"]`; carrying them here made the workflow
                # input scale with the city. Retained as a key so a run submitted
                # with the old shape and resumed after this change still resolves.
                "features": [],
                "deps": deps_of.get(unit.indicator, []),
            }
            for unit in plan.units
        ]
        params: dbos_app.RunParams = {
            "code_version": code_version,
            "scenario_id": scenario_id,
            "aoi_id": aoi_id,
            # Committed before the workflow is enqueued; see `_persist_plan`.
            "plan_id": _persist_plan(plan.units),
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

            try:
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
            except Exception as exc:  # noqa: BLE001 — re-raised unless it is 42P01
                # The write-side twin of the 42P01 handling in `_from_workflow`,
                # and the reason that one was not enough: a client never creates
                # this schema, so on a database no worker has launched against,
                # *reads* correctly answer "no such run" while every submit dies
                # on an INSERT into a table that does not exist. Untranslated,
                # that reaches the API as a SQLAlchemy error and is reported as
                # an internal fault — sending whoever reads it hunting for a bug
                # in shipped SQL, when the actual answer is that a provisioning
                # step was skipped. Bootstrap provisions it now; this is the
                # layer that stays correct when something bypasses bootstrap.
                #
                # Matched by SQLSTATE, not exception class, for the same reason
                # as the read path: the error arrives wrapped by SQLAlchemy
                # around a psycopg3 error, and psycopg3 is a transitive
                # dependency this package never declared.
                if getattr(getattr(exc, "orig", None), "sqlstate", None) != "42P01":
                    raise
                raise OpsPlaneNotProvisioned() from exc
        else:
            # ``dev`` intentionally self-executes; an executor process does
            # the same when it is also an interactive submitter.
            dbos_app.launch()
            from dbos import SetWorkflowID

            with SetWorkflowID(run_id):
                # DBOS types the payload as a plain dict; RunParams is a
                # TypedDict, which is structurally compatible but not nominally.
                dbos_app.runs_queue().enqueue(
                    dbos_app.run_workflow,
                    run_id,
                    units,
                    params,  # type: ignore[arg-type]
                )

        return RunHandle(run_id=run_id, status=None)

    # ── Read ────────────────────────────────────────────────────────────────

    def get_run(self, conn: DBConnection, run_id: str) -> RunStatus | None:
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
        try:
            found = _client().list_workflows(workflow_ids=[run_id])
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is 42P01
            # DBOS owns its schema and creates it at `launch()`, so on a database
            # it has never launched against `dbos.workflow_status` does not
            # exist. That is not an error to propagate: no workflow can exist
            # either, and the answer to "does this run exist" is no. The port
            # promises `RunStatus | None`, and leaking `UndefinedTable` through
            # it is the same contract break the other track avoids by mapping
            # its engine's not-found onto None.
            #
            # Matched by SQLSTATE rather than exception class on purpose: the
            # error arrives wrapped by SQLAlchemy around a psycopg3 error, and
            # psycopg3 reaches us only transitively through DBOS — importing it
            # here would be depending on something this package never declared.
            if getattr(getattr(exc, "orig", None), "sqlstate", None) != "42P01":
                raise
            return None
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

    def cancel_run(self, conn: DBConnection, run_id: str) -> bool:
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

        ⚠ The slot is held by the **thread**, and released in ``_pump``'s own
        ``finally`` — not by this generator. Releasing it here, which is what
        this did, made the limit count live *subscriptions* rather than live
        threads, and those are not the same number for exactly the reason the
        limit exists: a client that disconnects one second into a twenty-minute
        run ends this generator immediately while its pump stays blocked in
        ``read_stream`` for the remaining nineteen. The freed slot then admits
        the next subscriber, which starts another thread, so the count of
        uninterruptible threads was bounded by nothing at all — the failure the
        semaphore was added to prevent.
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
                # Released here, where the thread actually ends. See the
                # docstring: this is the whole point of the slot.
                _PROGRESS_SLOTS.release()
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_offer, done)

        thread = _threading.Thread(
            target=_pump, name=f"dbos-progress-{run_id[:8]}", daemon=True
        )
        try:
            thread.start()
        except BaseException:
            # The slot is released by the pump, so a thread that never starts
            # would hold it for the life of the process.
            _PROGRESS_SLOTS.release()
            raise
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
            # produces something or closes — which is why the slot is released
            # there and not here.
            stop.set()

    def _snapshot(self, run_id: str) -> RunEvent | None:
        """The run-level status, read without the caller's connection.

        The port hands ``subscribe_progress`` no connection — no engine's
        progress transport is the caller's pooled one — so this opens its own.

        ⚠ ``_closing``, not a bare ``with connect(...)``. psycopg2's connection
        context manager scopes the transaction and leaves the socket open, and
        this runs once per subscriber in a long-lived API process — the same
        leak ``_pg`` and ``_persist_plan`` already spell out.
        """
        from reimagina.db import connect

        from reimagina.orchestration import dbos_app

        with _closing(connect(dbos_app.dbos_dsn())) as conn:
            row = runstore.get_run(conn, run_id)
        return RunEvent(
            run_id=run_id,
            indicator=None,
            status=row["status"] if row else "unknown",
        )
