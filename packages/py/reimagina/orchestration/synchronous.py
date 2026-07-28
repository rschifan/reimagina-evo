"""Synchronous in-process orchestrator — the reference implementation of the port.

Runs the impact engine + coordinator inline. The run is complete when
``submit_run`` returns, so the ``RunHandle`` carries the final status.

Run state goes to an injected store, defaulting to an in-memory one. It used to
write ``jobs_runtime`` directly, which made this module a hybrid — domain
sequencing plus ops-plane persistence — and coupled the port's reference
implementation to a schema an engine-owned ops-plane may drop. Pass
``store=runstore`` to get the old persisting behaviour back; the signatures are
interchangeable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.coordinator import Runner, run_dirty_set
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.memstore import InMemoryRunStore
from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView
from reimagina.orchestration.runners import default_runners


class SynchronousOrchestrator:
    """Port implementation that computes the dirty-set and runs it in-process."""

    def __init__(
        self,
        *,
        registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
        runners: Mapping[str, Runner] | None = None,
        store: Any | None = None,
    ) -> None:
        self._registry = registry
        self._runners = runners
        self._store = store if store is not None else InMemoryRunStore()

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
        runners = (
            default_runners(self._registry) if self._runners is None else self._runners
        )
        run_id = str(uuid4())
        self._store.create_run(
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
        self._store.record_units(conn, run_id=run_id, units=units)
        self._store.finalize_run(conn, run_id=run_id, status=report.status)

        status = RunStatus(
            run_id=run_id,
            status=report.status,
            units=tuple(
                UnitStatusView(u["indicator"], u["status"], u["detail"]) for u in units
            ),
        )
        return RunHandle(run_id=run_id, status=status)

    def get_run(self, conn: object, run_id: str) -> RunStatus | None:
        row = self._store.get_run(conn, run_id)
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
