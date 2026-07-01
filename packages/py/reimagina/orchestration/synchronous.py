"""Synchronous in-process orchestrator — the rung-1 implementation of the port.

Runs the impact engine + coordinator inline and persists the per-unit outcome to
``jobs_runtime`` (Postgres; a no-op on DuckDB). The run is complete when
``submit_run`` returns, so the ``RunHandle`` carries the final status. This makes
the synchronous path *an implementation of the port* rather than the only entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration import runstore
from reimagina.orchestration.coordinator import Runner, run_dirty_set
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView
from reimagina.orchestration.runners import default_runners


class SynchronousOrchestrator:
    """Port implementation that computes the dirty-set and runs it in-process."""

    def __init__(
        self,
        *,
        registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
        runners: Mapping[str, Runner] | None = None,
    ) -> None:
        self._registry = registry
        self._runners = runners

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
        runners = default_runners() if self._runners is None else self._runners
        run_id = str(uuid4())
        runstore.create_run(
            conn, run_id=run_id, scenario_id=scenario_id, code_version=code_version
        )

        dirty = compute_dirty_set(changed_attributes, edited_features, self._registry)
        report = run_dirty_set(
            conn,
            dirty,
            self._registry,
            runners,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
        )

        by_indicator = report.by_indicator()
        units = [
            {
                "indicator": unit.indicator,
                "features": sorted(unit.features),
                "status": by_indicator[unit.indicator].status.value,
                "detail": by_indicator[unit.indicator].detail,
            }
            for unit in dirty
        ]
        runstore.record_units(conn, run_id=run_id, units=units)
        runstore.finalize_run(conn, run_id=run_id, status=report.status)

        status = RunStatus(
            run_id=run_id,
            status=report.status,
            units=tuple(
                UnitStatusView(u["indicator"], u["status"], u["detail"]) for u in units
            ),
        )
        return RunHandle(run_id=run_id, status=status)

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
