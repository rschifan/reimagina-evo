"""Coordinator — the in-process executor and the graph derivation it shares.

- ``run_dirty_set`` — the single-process, in-memory executor: runs each
  indicator's runner in dependency order, skipping any whose dirty inputs
  didn't succeed, and returns a per-unit + overall status (F6 partial-success).
  This is the ``--sync`` path and survives every ops-plane choice.
- ``_dep_map`` — slug → its input slugs, derived from the registry. Shared with
  the wave scheduler.

The distributed readiness functions (``plan_wave`` / ``next_ready_units``) moved
to ``reimagina.orchestration.wave``: an engine that owns scheduling natively
deletes that module, and keeping it here would make the split a diff inside a
file rather than a file. The status vocabulary lives in
``reimagina.orchestration.port``. Imports flow wave → coordinator → port.

Runners (slug → compute fn) are injected, not defined here — that binding is
``reimagina.orchestration.runners`` (component 7). A runner owns its own
job_run row (provenance lives where the compute happens); the RunReport is the
in-memory view across units.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.impact import DirtyUnit
from reimagina.orchestration.port import overall_status

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
        """Overall run status — one rule, shared with the async distributor.

        No outcomes means the dirty-set was empty, which is ``noop``, not a
        vacuous success: nothing was invalidated, so no run happened. The async
        orchestrators already answered that way, and reporting ``succeeded``
        here made the same edit look different through ``--sync``.

        ``overall_status`` itself is unchanged — it rolls up *unit* statuses and
        feeds ``orchestration_run.status``, whose CHECK has no ``noop``. A
        persisted run always has at least one unit, so it never sees the empty
        case.
        """
        if not self.outcomes:
            return "noop"
        return overall_status(o.status.value for o in self.outcomes)

    def by_indicator(self) -> dict[str, UnitOutcome]:
        return {o.indicator: o for o in self.outcomes}


def _dep_map(registry: Sequence[IndicatorSpec]) -> dict[str, set[str]]:
    """slug -> the slugs it depends on (its inputs)."""
    return {spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry}


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
