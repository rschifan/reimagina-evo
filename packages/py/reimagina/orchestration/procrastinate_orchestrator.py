"""Async orchestrator — the Procrastinate-backed implementation of the port.

``submit_run`` writes the run envelope + one queued job_unit per dirty unit,
commits (so the worker's separate connection sees them), then defers the first
``advance_run``. It returns immediately — the caller polls ``get_run``. The
caller must pass a commit-safe connection (not the API's rollback-scoped pool).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import runstore
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView
from reimagina.orchestration.queue_app import app, queue_dsn
from reimagina.orchestration.tasks import advance_run


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
        runstore.create_run(
            conn,
            run_id=run_id,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            status="running",
        )
        dirty = compute_dirty_set(changed_attributes, edited_features, self._registry)
        runstore.record_units(
            conn,
            run_id=run_id,
            units=[
                {
                    "indicator": u.indicator,
                    "features": sorted(u.features),
                    "status": "queued",
                }
                for u in dirty
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
