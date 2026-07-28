"""Intake — the single entry point that evaluates a scenario (component 2).

``evaluate_scenario`` is the one call the application, CLI, and API depend on
to turn a scenario edit into recomputed results: it composes the impact engine
(what to recompute) and the coordinator (run it). It is also the seam the whole
engine sits behind — when a second orchestrator arrives (the Dagster adapter in
PHASE3_ORCHESTRATION_EXPLORATION.md §4), this signature is what it implements,
so a formal port (component 13) gets extracted then, not speculatively now.

Rung-1 scope: synchronous — the coordinator runs in-process before the call
returns. The design's "cheap intake / no compute in the request path" and
run-level dedup (refuse a duplicate run for a scenario already computing) both
belong to the async queue rung (rung 2); there is nothing to dedup against
while runs are synchronous.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.coordinator import RunReport, Runner, run_dirty_set
from reimagina.orchestration.plan import plan_run
from reimagina.orchestration.runners import default_runners


def evaluate_scenario(
    conn: object,
    *,
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
    runners: Mapping[str, Runner] | None = None,
    code_version: str = "dev",
) -> RunReport:
    """Evaluate a scenario edit end to end and return the run report.

    Given the attributes the scenario changed and the features it changed them
    on, compute the dirty-set and run it through the coordinator. ``runners``
    defaults to ``default_runners()`` (the shipped indicators); ``registry``
    defaults to the shipped catalog.

    An empty dirty-set is reported as ``noop`` by the returned report, the same
    word the async orchestrators use — see ``plan.plan_run``.
    """
    plan = plan_run(
        conn,
        changed_attributes=changed_attributes,
        edited_features=edited_features,
        scenario_id=scenario_id,
        aoi_id=aoi_id,
        code_version=code_version,
        registry=registry,
    )
    return run_dirty_set(
        conn,
        plan.units,
        registry,
        default_runners() if runners is None else runners,
        **plan.params,
    )
