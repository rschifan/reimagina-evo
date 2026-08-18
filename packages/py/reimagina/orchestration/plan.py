"""The submit prelude — everything between "a scenario changed" and "run it".

Resolving the effective change-set and deriving the dirty-set is needed by
every entry point and by every ops-plane: an engine replaces the *execution* of
a run, never the decision of what the run contains. Extracting it here is why
swapping the ops-plane did not touch the dirty-set logic at all.

It is a module of its own rather than a function in ``intake`` because both are
true at once — the sync path and every async engine call it — so it must not
live inside either. Nothing here touches a queue, a worker or a run table.

**Noop.** An empty dirty-set means nothing was invalidated, which is not the
same as a run that succeeded at doing nothing. Both callers had their own
opinion: the async path returned a distinct ``noop`` while the sync path logged
a warning and reported ``succeeded``, so the same edit answered differently
depending on which entry point you used. The decision lives here now, once, and
``noop_handle`` is the one shape a caller returns for it.

``noop`` is deliberately **return-only** — ``orchestration_run.status`` has no
such value (``r028_orchestration_run_and_job_unit``) and a noop persists no run
to give it to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.impact import DirtyUnit, compute_dirty_set
from reimagina.orchestration.port import RunHandle, RunStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunPlan:
    """What a run will do, before anything decides how to execute it."""

    units: list[DirtyUnit]
    # scenario_id / aoi_id / code_version — the run-level arguments every
    # executor forwards to its runners. Kept as a dict because both
    # ``run_dirty_set`` and ``runstore.create_run`` accept exactly these as
    # keywords, and an engine has to serialize them onto a queue.
    params: dict[str, Any]

    @property
    def is_noop(self) -> bool:
        return not self.units


def plan_run(
    conn: object,
    *,
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
) -> RunPlan:
    """Resolve the change-set and derive the dirty-set, in dependency order.

    Reads the scenario's overrides when the change-set is not given explicitly,
    so a caller can submit ``scenario_id`` alone and have the diff derived from
    the override rows.
    """
    from reimagina.scenarios.overrides import resolve_change_set

    changed, edited = resolve_change_set(
        conn,
        changed_attributes=changed_attributes,
        edited_features=edited_features,
        scenario_id=scenario_id,
    )
    units = compute_dirty_set(changed, edited, registry)
    if not units:
        logger.warning(
            "scenario %s produced an empty dirty-set — nothing to recompute "
            "(check changed_attributes / scenario overrides)",
            scenario_id,
        )
    return RunPlan(
        units=units,
        params={
            "scenario_id": scenario_id,
            "aoi_id": aoi_id,
            "code_version": code_version,
        },
    )


def noop_handle(run_id: str) -> RunHandle:
    """The handle a port implementation returns when the plan was a noop.

    The id is minted and returned for log correlation, but no run exists to
    look up — which is why the API suppresses it (``jobs._submit_response``)
    rather than handing back an id that 404s.
    """
    return RunHandle(
        run_id=run_id, status=RunStatus(run_id=run_id, status="noop", units=())
    )
