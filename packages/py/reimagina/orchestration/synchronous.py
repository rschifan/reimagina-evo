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

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.coordinator import Runner, run_dirty_set
from reimagina.orchestration.memstore import InMemoryRunStore
from reimagina.orchestration.plan import noop_handle, plan_run
from reimagina.orchestration.port import (
    DBConnection,
    RunEvent,
    RunHandle,
    RunStatus,
    UnitStatusView,
)
from reimagina.orchestration.runners import default_runners


def _detail(value: object) -> str | None:
    """Store rows are loosely typed; detail is optional free text."""
    return None if value is None else str(value)


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
        # run_id -> terminal status, for subscribe_progress. It cannot read the
        # store: the port gives it no connection (every other engine's progress
        # transport is its own), and a Postgres-backed store needs one.
        self._outcomes: dict[str, str] = {}

    def submit_run(
        self,
        conn: DBConnection,
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
            # Same answer the async orchestrators give, and for the same reason:
            # persisting a run row here would need a 'noop' the status CHECK
            # does not have.
            return noop_handle(run_id)

        self._store.create_run(conn, run_id=run_id, **plan.params)
        report = run_dirty_set(
            conn, plan.units, self._registry, runners, run_id=run_id, **plan.params
        )

        by_indicator = report.by_indicator()
        units = [
            {
                "indicator": unit.indicator,
                "features": sorted(unit.features),
                "status": by_indicator[unit.indicator].status.value,
                "detail": by_indicator[unit.indicator].detail,
            }
            for unit in plan.units
        ]
        self._store.record_units(conn, run_id=run_id, units=units)
        self._store.finalize_run(conn, run_id=run_id, status=report.status)
        self._outcomes[run_id] = report.status

        status = RunStatus(
            run_id=run_id,
            status=report.status,
            units=tuple(
                UnitStatusView(
                    str(u["indicator"]), str(u["status"]), _detail(u["detail"])
                )
                for u in units
            ),
        )
        return RunHandle(run_id=run_id, status=status)

    def get_run(self, conn: DBConnection, run_id: str) -> RunStatus | None:
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

    def cancel_run(self, conn: DBConnection, run_id: str) -> bool:
        """Always False — a run here is already over by the time anyone has its id.

        ``submit_run`` does not return until the last runner has, so there is no
        window in which a caller holds a run_id for something still in flight.
        """
        return False

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """One run-level event with the final status, then stop.

        The run is complete before its id escapes, so a subscription can only
        ever report history. It yields the snapshot every implementation opens
        with and closes immediately rather than pretending to stream.

        Answers from runs *this instance* ran, not from the store. The port
        hands ``subscribe_progress`` no connection — deliberately, since no
        other engine's progress transport is the caller's pooled connection —
        so a Postgres-backed store could never be read here, and asking it with
        a None connection silently returned ``unknown`` for every run,
        including ones it had just finished.
        """
        yield RunEvent(
            run_id=run_id,
            indicator=None,
            status=self._outcomes.get(run_id, "unknown"),
        )
