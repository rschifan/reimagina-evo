"""Coordinator — sequences a dirty-set into actual recompute (component 8).

Rung 1 of the ops-plane ladder in
docs/product/PHASE3_ORCHESTRATION_EXPLORATION.md §4: a **single-process,
in-memory** coordinator. It takes the ordered dirty-set from the impact engine
and runs each indicator's runner in dependency order, skipping any whose dirty
inputs didn't succeed, and reports a per-unit outcome plus an overall status
(succeeded / partial / failed — F6 partial-success).

What it deliberately is *not*, yet:
- No work queue / `SKIP LOCKED` pop and no durable per-unit task table — those
  are higher rungs (procrastinate/PgQueuer → Hatchet), pulled in when multiple
  workers must pull units concurrently. Here the coordinator calls runners
  directly, in process.
- It does not open job_run rows: each runner owns its own job_run (see
  reimagina.indicators.compactness), so run-tracking + provenance already live
  where the compute happens. The RunReport is the in-memory view across units.

The runner registry (which slug maps to which compute fn) is injected, not
defined here — that wiring is component 7 (worker pools), the next step.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.impact import DirtyUnit

logger = logging.getLogger(__name__)


class UnitStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Runner(Protocol):
    """Computes one indicator's outputs for a set of features.

    A runner owns its own job_run row and result writes; the coordinator only
    sequences it. ``features`` is the dirty feature set (empty = all applicable);
    rung-1 compute fns are AOI-scoped and may ignore it until feature-level
    scoping lands.
    """

    def __call__(
        self,
        conn: object,
        *,
        features: frozenset[str],
        scenario_id: str | None,
        code_version: str,
        aoi_id: str | None,
    ) -> object: ...


@dataclass(frozen=True)
class UnitOutcome:
    indicator: str
    status: UnitStatus
    detail: str | None = None  # error message or skip reason


@dataclass(frozen=True)
class RunReport:
    outcomes: tuple[UnitOutcome, ...]

    @property
    def status(self) -> str:
        """Overall run status: succeeded (all ok), failed (none ok), else partial."""
        seen = {o.status for o in self.outcomes}
        if not seen or seen == {UnitStatus.SUCCEEDED}:
            return "succeeded"
        if UnitStatus.SUCCEEDED in seen:
            return "partial"
        return "failed"

    def by_indicator(self) -> dict[str, UnitOutcome]:
        return {o.indicator: o for o in self.outcomes}


def _dep_map(registry: Sequence[IndicatorSpec]) -> dict[str, set[str]]:
    """slug -> the slugs it depends on (its inputs)."""
    return {spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry}


def next_ready_units(
    dirty_units: Sequence[DirtyUnit],
    registry: Sequence[IndicatorSpec],
    finished: Mapping[str, UnitStatus],
) -> list[DirtyUnit]:
    """Units dispatchable now, given the units already finished.

    Shared readiness function for the async distributor (component 8): a unit is
    ready when it has not finished yet and every one of its *dirty* inputs has
    finished with ``SUCCEEDED``. A unit whose dirty input failed/skipped is never
    ready — the caller marks it skipped. ``run_dirty_set`` applies the same rule
    inline while walking the pre-sorted list.
    """
    deps = _dep_map(registry)
    dirty = {u.indicator for u in dirty_units}
    return [
        unit
        for unit in dirty_units
        if unit.indicator not in finished
        and all(
            finished.get(dep) is UnitStatus.SUCCEEDED
            for dep in deps.get(unit.indicator, set()) & dirty
        )
    ]


def run_dirty_set(
    conn: object,
    dirty_units: Sequence[DirtyUnit],
    registry: Sequence[IndicatorSpec],
    runners: Mapping[str, Runner],
    *,
    scenario_id: str | None = None,
    code_version: str = "dev",
    aoi_id: str | None = None,
) -> RunReport:
    """Run the dirty-set in dependency order; return per-unit + overall status.

    ``dirty_units`` is the already-topologically-ordered output of
    ``compute_dirty_set`` (inputs precede dependents). For each unit:

    - if any of its *dirty* inputs did not succeed → **skipped** (a composite
      can't be trusted when an input it depends on failed; non-dirty inputs
      keep their current value and don't block);
    - if no runner is registered for it → **skipped** (nothing to execute);
    - otherwise run it; an exception isolates to that unit → **failed**, and
      its dependents skip on the next iterations.

    Independent units don't block each other — a failure only stops the units
    that depend on it.
    """
    deps = _dep_map(registry)
    dirty = {u.indicator for u in dirty_units}
    outcomes: dict[str, UnitOutcome] = {}

    for unit in dirty_units:
        slug = unit.indicator
        unsatisfied = sorted(
            dep
            for dep in deps.get(slug, set()) & dirty
            if outcomes.get(dep) is None
            or outcomes[dep].status is not UnitStatus.SUCCEEDED
        )
        if unsatisfied:
            outcomes[slug] = UnitOutcome(
                slug,
                UnitStatus.SKIPPED,
                f"unsatisfied input(s): {', '.join(unsatisfied)}",
            )
            continue

        runner = runners.get(slug)
        if runner is None:
            outcomes[slug] = UnitOutcome(
                slug, UnitStatus.SKIPPED, "no runner registered"
            )
            continue

        try:
            runner(
                conn,
                features=unit.features,
                scenario_id=scenario_id,
                code_version=code_version,
                aoi_id=aoi_id,
            )
            outcomes[slug] = UnitOutcome(slug, UnitStatus.SUCCEEDED)
        except Exception as exc:  # noqa: BLE001 — fault isolation is the point: one unit's failure must not abort the run, only its dependents.
            logger.exception("indicator %s failed", slug)
            outcomes[slug] = UnitOutcome(slug, UnitStatus.FAILED, str(exc))

    return RunReport(tuple(outcomes[u.indicator] for u in dirty_units))
