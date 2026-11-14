"""Intake — evaluate a scenario edit in-process and return the per-unit report.

Composes the impact engine (what to recompute) with the coordinator (run it),
synchronously: everything has finished by the time the call returns.

This is **not** the general entry point any more. The API and the CLI's queue
modes go through ``orchestration.port.Orchestrator`` via
``orchestration.factory.get_orchestrator``; the only caller left here is the
CLI's ``--sync`` mode, which wants the coordinator's richer ``RunReport`` (one
outcome per unit, with detail) rather than the port's ``RunStatus``.

It survives every ops-plane choice precisely because it needs no ops-plane: no
queue, no worker, no run tables. That makes it the one path still runnable when
the engine is down, and the reason ``coordinator.run_dirty_set`` is port-level
rather than engine-level code.

Run-level dedup now exists, in ``orchestration.dedup`` and the DBOS adapter:
a submission carrying work already outstanding is answered with the run doing
it. **This path is not covered by it.** ``evaluate_scenario`` computes in the
calling process and returns when it is done, so there is no window in which a
second call could find the first still outstanding — the deduplication would
have nothing to collapse onto. That is a property of synchronous execution,
not an omission.
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
