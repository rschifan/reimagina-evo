"""Async orchestrator — the Procrastinate-backed implementation of the port.

``submit_run`` writes the run envelope + one queued job_unit per dirty unit,
commits (so the worker's separate connection sees them), then defers the first
``advance_run``. It returns immediately — the caller polls ``get_run``. The
caller must pass a commit-safe connection (not the API's rollback-scoped pool).

``subscribe_progress`` is this engine's progress transport: Postgres
LISTEN/NOTIFY on its own direct connection, since LISTEN cannot cross PgBouncer
in transaction mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from uuid import uuid4

import psycopg2

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import events, runstore
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
from reimagina.orchestration.queue_app import app, bound, queue_dsn
from reimagina.orchestration.tasks import advance_run
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)


class ProcrastinateOrchestrator:
    def __init__(
        self, *, registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS
    ) -> None:
        self._registry = registry

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
        queue_dsn()  # fail loudly now if DATABASE_URL_DIRECT is unset
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
            # Nothing to recompute — a distinct status rather than a green run
            # that silently did nothing, and nothing persisted to hand back.
            return noop_handle(run_id)

        runstore.create_run(conn, run_id=run_id, status="running", **plan.params)
        runstore.record_units(
            conn,
            run_id=run_id,
            units=[
                {
                    "indicator": u.indicator,
                    "features": sorted(u.features),
                    "status": "queued",
                }
                for u in plan.units
            ],
        )
        conn.commit()  # worker runs on its own connection; it must see the rows

        # `bound()` rather than a bare `app.open()`: the module-global
        # connector may have captured an empty DSN at import time.
        with bound(), app.open():
            advance_run.configure(queueing_lock=f"advance:{run_id}").defer(
                run_id=run_id
            )

        return RunHandle(run_id=run_id, status=None)

    def get_run(self, conn: object, run_id: str) -> RunStatus | None:
        row = runstore.get_run(conn, run_id)
        if row is None:
            return None
        return RunStatus(
            run_id=row["run_id"],
            status=row["status"],
            units=tuple(
                UnitStatusView(u["indicator"], u["status"], u["detail"])
                for u in row["units"]
            ),
        )

    def cancel_run(self, conn: object, run_id: str) -> bool:
        """Stop the run: cancel its live backends, then close its units.

        Two mechanisms, because one is not enough. Marking units ``cancelled``
        stops *scheduling* — ``plan_wave`` treats it as terminal, so the next
        ``advance_run`` finds nothing ready — but the worker already executing a
        unit holds its own connection and would run to completion; nothing in
        Python interrupts a blocking PostGIS call. ``pg_cancel_backend`` is what
        actually stops the statement (r031).

        The worker still finishes its own error handling afterwards and tries to
        record ``failed``; ``runstore.set_unit_status`` refuses to move a unit
        out of a terminal state, which is what keeps ``cancelled`` from being
        overwritten.
        """
        row = runstore.get_run(conn, run_id)
        if row is None or row["status"] in RUN_TERMINAL_STATES:
            return False

        backend_for_connection(conn).job_run_repo().cancel_run_backends(run_id)

        for unit in row["units"]:
            if unit["status"] in UNIT_TERMINAL_STATES:
                continue
            if runstore.set_unit_status(
                conn,
                run_id=run_id,
                indicator_slug=unit["indicator"],
                status="cancelled",
                detail="run cancelled",
            ):
                events.emit(
                    conn, run_id=run_id, indicator=unit["indicator"], status="cancelled"
                )

        # Roll up from a fresh read, not from the snapshot above. A worker can
        # commit `succeeded` between the two, and set_unit_status then refuses
        # our `cancelled` — so assuming the write landed would report a run
        # cancelled whose units all say they finished.
        final = overall_status(u["status"] for u in runstore.list_units(conn, run_id))
        if runstore.finalize_run(conn, run_id=run_id, status=final):
            events.emit(conn, run_id=run_id, indicator=None, status=final)
        conn.commit()
        return True

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Snapshot, then LISTEN/NOTIFY updates until the run goes terminal.

        Opens its own direct connection — LISTEN needs one that is not
        transaction-pooled, and the caller's pooled connection is neither
        long-lived nor safe to block on.

        The connect is synchronous on purpose. Doing it in a thread creates a
        window where the subscriber is cancelled mid-connect: the thread cannot
        be interrupted, so the socket still opens, but the generator's cleanup
        has already run and nothing ever closes it. One local connect is cheap
        enough that removing the window is the better trade.
        """
        conn = psycopg2.connect(queue_dsn())
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"LISTEN {events.CHANNEL}")

            # Snapshot AFTER LISTEN, so a finalise landing between the two is
            # caught by one or the other rather than falling down the gap.
            current = runstore.get_run(conn, run_id)
            status = current["status"] if current else "unknown"
            yield RunEvent(run_id=run_id, indicator=None, status=status)
            if status in RUN_TERMINAL_STATES:
                return

            # The event loop watches the socket directly. Waiting in a thread
            # instead would leave that thread blocked in select() on this
            # connection's fd while cancellation ran the cleanup below and
            # closed it — and Postgres hands the freed fd straight to the next
            # connection in the process, so the stray select lands on someone
            # else's socket.
            loop = asyncio.get_running_loop()
            readable = asyncio.Event()
            loop.add_reader(conn.fileno(), readable.set)
            try:
                while True:
                    await readable.wait()
                    readable.clear()
                    conn.poll()
                    while conn.notifies:
                        note = conn.notifies.pop(0)
                        data = json.loads(note.payload)
                        if data.get("run_id") != run_id:
                            continue
                        indicator = data.get("indicator")
                        status = data.get("status", "")
                        yield RunEvent(
                            run_id=run_id, indicator=indicator, status=status
                        )
                        if indicator is None and status in RUN_TERMINAL_STATES:
                            return
            finally:
                loop.remove_reader(conn.fileno())
        finally:
            conn.close()
