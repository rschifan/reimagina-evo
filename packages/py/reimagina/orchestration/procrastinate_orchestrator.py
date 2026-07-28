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
import select
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
from reimagina.orchestration.queue_app import app, queue_dsn
from reimagina.orchestration.tasks import advance_run

logger = logging.getLogger(__name__)

# How long one wait on the LISTEN socket blocks before looping. Only bounds how
# promptly the subscription notices it has been closed — the caller runs its own
# heartbeat clock, so this is not the keep-alive interval.
_POLL_SECONDS = 1.0


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

        with app.open():
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
        """Mark every non-terminal unit ``cancelled`` and close the run.

        A unit already executing keeps running: the worker holds a separate
        connection and nothing here interrupts a blocking PostGIS call. What
        this does guarantee is that no *further* wave is dispatched —
        ``plan_wave`` treats ``cancelled`` as terminal, so the next
        ``advance_run`` finds nothing ready and the run stays closed.
        """
        row = runstore.get_run(conn, run_id)
        if row is None or row["status"] in RUN_TERMINAL_STATES:
            return False

        statuses = []
        for unit in row["units"]:
            status = unit["status"]
            if status not in UNIT_TERMINAL_STATES:
                status = "cancelled"
                runstore.set_unit_status(
                    conn,
                    run_id=run_id,
                    indicator_slug=unit["indicator"],
                    status=status,
                    detail="run cancelled",
                )
                events.emit(
                    conn, run_id=run_id, indicator=unit["indicator"], status=status
                )
            statuses.append(status)

        final = overall_status(statuses)
        runstore.finalize_run(conn, run_id=run_id, status=final)
        events.emit(conn, run_id=run_id, indicator=None, status=final)
        conn.commit()
        return True

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Snapshot, then LISTEN/NOTIFY updates until the run goes terminal.

        Opens its own direct connection — LISTEN needs one that is not
        transaction-pooled, and the caller's pooled connection is neither
        long-lived nor safe to block on.
        """
        conn = await asyncio.to_thread(psycopg2.connect, queue_dsn())
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

            while True:
                ready, _, _ = await asyncio.to_thread(
                    select.select, [conn], [], [], _POLL_SECONDS
                )
                if not ready:
                    continue  # the caller owns the keep-alive clock, not us
                conn.poll()
                while conn.notifies:
                    note = conn.notifies.pop(0)
                    data = json.loads(note.payload)
                    if data.get("run_id") != run_id:
                        continue
                    indicator = data.get("indicator")
                    status = data.get("status", "")
                    yield RunEvent(run_id=run_id, indicator=indicator, status=status)
                    if indicator is None and status in RUN_TERMINAL_STATES:
                        return
        finally:
            conn.close()
