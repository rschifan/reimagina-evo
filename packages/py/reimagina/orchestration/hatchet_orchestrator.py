"""The Orchestrator port over Hatchet.

This is where the track's central trade becomes concrete. The engine owns run
state, so ``get_run`` does not read a table we wrote — it reads the engine's and
translates. Everything the engine's vocabulary cannot say is lost here, and the
losses are listed in ``_RUN_STATUS_FOR`` and ``_UNIT_STATUS_FOR`` rather than
hidden in the mapping code.

**The run id is Hatchet's workflow run id.** There is no ``orchestration_run``
table on this branch to hold a mapping, and inventing one would be re-adopting
the state model this track exists to give away. The workflow learns its own id
from ``ctx.workflow_run_id`` at execution time, which is why ``RunInput.run_id``
is not passed in by the submitter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.plan import noop_handle, plan_run
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    RunEvent,
    RunHandle,
    RunStatus,
    UnitStatusView,
)
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)

# ── What the engine's vocabulary costs us ───────────────────────────────────
#
# Measured in spike B7, not inferred. Hatchet's enum is
# QUEUED | RUNNING | COMPLETED | CANCELLED | FAILED — there is no `partial` and
# no `skipped`, so two distinctions the library track keeps are gone:
#
#   * a run where some units succeeded and one failed reports FAILED, with no
#     way to say the rest worked. Callers cannot tell "retry everything" from
#     "retry one unit".
#   * a unit skipped because it was not dirty reports COMPLETED — identical to
#     one that actually computed. Only `ctx.was_skipped` or the engine's
#     internal `initial_state` column can tell them apart, and neither is
#     visible through the REST status this maps from.
#
# Both are the accepted cost of letting the engine own run state. They are
# stated here rather than smoothed over, because a mapping that quietly
# invented `partial` would be reporting something the engine does not know.

_RUN_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETED": "succeeded",
    "CANCELLED": "cancelled",
    # ⚠ Not `partial`. A run with one failed unit and three succeeded ones
    # arrives here as FAILED, and nothing in the engine's answer distinguishes
    # it from a run where everything failed.
    "FAILED": "failed",
}

_UNIT_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    # ⚠ Covers both "computed successfully" and "skipped because not dirty".
    "COMPLETED": "succeeded",
    # A dependent whose input failed lands here. The library track calls this
    # `skipped`, which says *why*; CANCELLED does not.
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}


class HatchetOrchestrator:
    """Scheduling and run state both belong to the engine."""

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
        from reimagina.orchestration import hatchet_app

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
            # A noop needs an id only for log correlation, and the engine will
            # never see this run — so this is the one id on this branch that is
            # ours rather than Hatchet's.
            from uuid import uuid4

            return noop_handle(str(uuid4()))

        # The dirty set is derived here, not inside `plan`. Same reasoning as
        # the library track: the catalog can change between submit and
        # execution, and a graph that re-derived its own work would disagree
        # with the one that was scheduled.
        payload = hatchet_app.RunInput(
            run_id="",  # filled from ctx.workflow_run_id; see the module docstring
            dirty=[u.indicator for u in plan.units],
            features={u.indicator: sorted(u.features) for u in plan.units},
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
        )
        ref = hatchet_app.run_workflow.run_no_wait(payload)
        return RunHandle(run_id=ref.workflow_run_id, status=None)

    # ── Read ────────────────────────────────────────────────────────────────

    def get_run(self, conn: object, run_id: str) -> RunStatus | None:
        """Translate the engine's answer. Nothing of ours is read.

        `conn` is unused, and that is the point: this branch has no run tables.
        The signature keeps it because the port must stay implementable by a
        track that does.
        """
        from reimagina.orchestration import hatchet_app

        try:
            details = hatchet_app.hatchet.runs.get(run_id)
        except Exception as exc:  # noqa: BLE001 — an unknown run is not an error
            logger.debug("run %s not found in the engine: %s", run_id, exc)
            return None
        if details is None:
            return None

        run = getattr(details, "run", details)
        engine_status = _name(getattr(run, "status", None))

        units: list[UnitStatusView] = []
        for task in getattr(details, "tasks", None) or []:
            slug = getattr(task, "display_name", None) or getattr(task, "step_id", "")
            slug = slug.rsplit(":", 1)[-1]  # "workflow:task" -> "task"
            if slug in ("plan", ""):
                continue  # scaffolding, not an indicator
            units.append(
                UnitStatusView(
                    slug,
                    _UNIT_STATUS_FOR.get(
                        _name(getattr(task, "status", None)), "queued"
                    ),
                    getattr(task, "error_message", None),
                )
            )

        return RunStatus(
            run_id=run_id,
            status=_RUN_STATUS_FOR.get(engine_status, "running"),
            units=tuple(sorted(units, key=lambda u: u.indicator)),
        )

    # ── Cancel ──────────────────────────────────────────────────────────────

    def cancel_run(self, conn: object, run_id: str) -> bool:
        """Ask the engine to stop, then stop the statement it cannot.

        Spike B4: cancelling marks the task CANCELLED immediately while the SQL
        keeps running, and the worker itself warns that its thread pool may
        block. So the engine's cancel is necessary and not sufficient —
        `pg_cancel_backend` against `job_run.backend_pid` (r031) is what
        actually ends the work.

        That table survives on every branch, which is the point worth carrying
        into the comparison: neither ops-plane solves this, and the answer lives
        outside both.
        """
        from reimagina.orchestration import hatchet_app

        current = self.get_run(conn, run_id)
        if current is None or current.status in RUN_TERMINAL_STATES:
            return False

        hatchet_app.hatchet.runs.cancel(run_id)
        backend_for_connection(conn).job_run_repo().cancel_run_backends(run_id)
        return True

    # ── Subscribe ───────────────────────────────────────────────────────────

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Follow the run, ending on a terminal status.

        ⚠ Polls rather than streaming. Hatchet has `subscribe_to_stream`, but it
        carries only whatever a task chose to `put_stream` — payloads with no
        task identity, on a call that swallows its own exceptions. It cannot
        answer "which indicator, what status", which is exactly what a
        `RunEvent` is. Reconstructing that would mean every task publishing its
        own identity, i.e. rebuilding the status vocabulary this track gave
        away.

        So the honest implementation is a poll, and the cost is real: the
        library track pushes ordered, offset-resumable events from the workflow
        itself. That difference belongs in the comparison.
        """
        seen: dict[str, str] = {}
        while True:
            status = await asyncio.to_thread(self.get_run, None, run_id)
            if status is None:
                yield RunEvent(run_id=run_id, indicator=None, status="unknown")
                return

            for unit in status.units:
                if seen.get(unit.indicator) != unit.status:
                    seen[unit.indicator] = unit.status
                    yield RunEvent(run_id, unit.indicator, unit.status, unit.detail)

            if status.status in RUN_TERMINAL_STATES:
                yield RunEvent(run_id, None, status.status)
                return
            await asyncio.sleep(_POLL_SECONDS)


_POLL_SECONDS = 1.0


def _name(value: object) -> str:
    """Enum member, bare string, or None -> a plain uppercase name."""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()
