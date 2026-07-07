"""Async orchestrator — the Procrastinate-backed implementation of the port.

``submit_run`` writes the run envelope + one queued job_unit per dirty unit,
commits (so the worker's separate connection sees them), then defers the first
``advance_run``. It returns immediately — the caller polls ``get_run``. The
caller must pass a commit-safe connection (not the API's rollback-scoped pool).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import runstore
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView
from reimagina.orchestration.queue_app import app, queue_dsn
from reimagina.orchestration.tasks import advance_run

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

        from reimagina.scenarios.overrides import resolve_change_set

        changed_attributes, edited_features = resolve_change_set(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
        )
        dirty = compute_dirty_set(changed_attributes, edited_features, self._registry)
        if not dirty:
            # Nothing to recompute — surface a distinct 'noop' instead of a
            # green run that silently did nothing. (Not persisted: the run
            # status CHECK has no 'noop'; return-only + log is the backstop.)
            logger.warning(
                "run for scenario %s produced an empty dirty-set — nothing to "
                "recompute (check changed_attributes / scenario overrides)",
                scenario_id,
            )
            return RunHandle(
                run_id=run_id,
                status=RunStatus(run_id=run_id, status="noop", units=()),
            )
        runstore.create_run(
            conn,
            run_id=run_id,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            status="running",
        )
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
