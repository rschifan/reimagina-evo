"""Wave scheduler — pure, DB-free readiness for a distributed ops-plane.

Split out of ``coordinator.py`` because this is the half an engine can replace.
An ops-plane that owns scheduling itself (a workflow engine declaring the
indicator DAG natively) deletes this module wholesale; one that only provides
durability keeps it and drives the waves from application code.

The in-process executor (``coordinator.run_dirty_set``) applies the same
readiness rule inline while walking a pre-sorted list, so the rule exists in two
shapes: once for "walk this ordered list", once for "given these statuses, what
moves now". ``coordinator`` owns the graph derivation (``_dep_map``) and ``port``
owns the status vocabulary; both flow one way into here, so there is no import
cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.coordinator import _dep_map
from reimagina.orchestration.port import (
    UNIT_TERMINAL_STATES,
    UNIT_UNSATISFIED_STATES,
    overall_status,
)

__all__ = ["WavePlan", "plan_wave"]


@dataclass(frozen=True)
class WavePlan:
    dispatch: tuple[str, ...]  # queued units whose inputs are satisfied → run now
    skip: tuple[str, ...]  # queued units with a failed/skipped input → mark skipped
    final_status: str | None  # set once every unit is terminal, else None


def plan_wave(
    units: Sequence[tuple[str, str]],
    registry: Sequence[IndicatorSpec],
) -> WavePlan:
    """Pure wave scheduler.

    ``units`` is ``(indicator_slug, status)`` for one run. Returns which queued
    units to dispatch (all dirty inputs succeeded), which to skip (a dirty input
    failed/skipped), and the run's final status once nothing is left running.
    Deterministic and DB-free so it is unit-tested directly; the caller wraps it
    with the SQL + enqueue.
    """
    deps = _dep_map(registry)
    all_slugs = {slug for slug, _ in units}
    finished = {slug: st for slug, st in units if st in UNIT_TERMINAL_STATES}

    dispatch: list[str] = []
    skip: list[str] = []
    for slug, status in units:
        if status != "queued":
            continue
        deps_of = deps.get(slug, set()) & all_slugs
        if any(finished.get(dep) in UNIT_UNSATISFIED_STATES for dep in deps_of):
            skip.append(slug)
        elif all(finished.get(dep) == "succeeded" for dep in deps_of):
            dispatch.append(slug)

    projected = {
        slug: ("skipped" if slug in skip else "running" if slug in dispatch else status)
        for slug, status in units
    }
    final = (
        overall_status(projected.values())
        if all(st in UNIT_TERMINAL_STATES for st in projected.values())
        else None
    )
    return WavePlan(tuple(dispatch), tuple(skip), final)
