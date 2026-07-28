"""Hatchet ops-plane — the engine owns scheduling and run state.

This module does **not** implement a scheduler. It declares the indicator graph
once, at import, and Hatchet does the rest: readiness, ordering, fan-out,
failure propagation, retries. That is the entire reason a service track exists,
and it is why ``orchestration/wave.py`` is deleted on this branch — the ~100
lines of wave scheduling we own on the library track become ~15 lines of
declaration here.

What is left to write is translation, not orchestration. Hatchet knows about
tasks and parents; it does not know what an indicator is, so something has to
map one onto the other. That mapping is `build_dag` below.

**Two behaviours we inherit rather than implement**, both measured (see
``docs/product/PHASE3_OPS_PLANE_HATCHET_SPIKES.md``):

* a dirty composite whose *non-dirty* input was skipped still runs — our
  "non-dirty inputs keep their current value and don't block" rule (spike B2);
* a failed unit cancels its dependents while independent branches finish and the
  run stays alive until they do — our F6 partial-success rule (spike B3).

**And two the engine imposes.** Its status enum has no ``partial`` and no
``skipped``, so a skipped indicator reports ``COMPLETED`` — indistinguishable
from one that computed — and a half-successful run reports ``FAILED`` (spike
B7). That is the accepted cost of letting the engine own run state.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from hatchet_sdk import Context, Hatchet, ParentCondition
from pydantic import BaseModel

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.jobs._lifecycle import current_run
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.runners import default_runners

# ⚠ Both Hatchet defaults are wrong for this workload, measured in spike B5.
#
# execution_timeout defaults to 60 SECONDS. Our units are PostGIS statements
# that run for minutes, so the default kills a healthy unit a minute in.
#
# schedule_timeout defaults to 5 minutes, and queued work that waits longer is
# reported FAILED — the same status as a unit whose compute broke. Under
# saturation that makes a busy period indistinguishable from an outage, so it
# must exceed slowest_unit × queued_depth ÷ slots.
UNIT_EXECUTION_TIMEOUT = timedelta(
    minutes=int(os.environ.get("REIMAGINA_UNIT_TIMEOUT_MIN", "120"))
)
UNIT_SCHEDULE_TIMEOUT = timedelta(
    hours=int(os.environ.get("REIMAGINA_QUEUE_TIMEOUT_HOURS", "6"))
)

hatchet = Hatchet()


class RunInput(BaseModel):
    """Everything the run needs, resolved before it is submitted.

    ``dirty`` is derived by the submitter, not by the ``plan`` task, for the
    same reason the library track passes it as a workflow input: the catalog can
    change mid-run, and a graph that re-derived its own work would disagree with
    the one that was scheduled.
    """

    run_id: str
    dirty: list[str] = []
    features: dict[str, list[str]] = {}
    scenario_id: str | None = None
    aoi_id: str | None = None
    code_version: str = "dev"


def _topo_order(registry: Sequence[IndicatorSpec]) -> list[IndicatorSpec]:
    """Parents before children — Hatchet needs the object to exist to reference it.

    Reuses the impact engine's ordering rather than re-deriving one, so the
    declaration order and the dirty-set order cannot drift. That ordering is
    deterministic across processes (a `sorted()` in ``impact.py``), which matters
    here: the API and the worker both build this graph, and two processes that
    disagreed about it would be running different workflows under one name.
    """
    everything = {s.slug for s in registry}
    ordered = compute_dirty_set({a for s in registry for a in s.reads}, (), registry)
    by_slug = {s.slug: s for s in registry}
    out = [by_slug[u.indicator] for u in ordered if u.indicator in everything]
    # Anything no attribute seeds still needs a node.
    out += [s for s in registry if s.slug not in {o.slug for o in out}]
    return out


def build_dag(registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS):
    """Declare one task per indicator. This is the whole scheduler.

    ``skip_if`` means *skip when the expression is true* — measured in spike B1,
    where the un-negated form ran exactly the indicators it should have left
    alone while every task reported COMPLETED. The negation is load-bearing.
    """
    workflow = hatchet.workflow(name="reimagina-run", input_validator=RunInput)

    @workflow.task(execution_timeout=timedelta(minutes=5))
    def plan(input: RunInput, ctx: Context) -> dict:
        """Publish the dirty set for the graph to read. Writes nothing.

        Returns slugs only — every child receives every parent's output, and
        `plan` parents every node, so this is the payload that scales. Slugs
        keep it at 72 KB even for a 2,000-indicator catalog (spike B6).
        """
        return {"dirty": input.dirty}

    node: dict[str, Any] = {"plan": plan}

    for spec in _topo_order(registry):
        parents = [plan] + [node[dep] for dep, _w in spec.depends_on if dep in node]

        def _runner(input: RunInput, ctx: Context, _slug: str = spec.slug) -> dict:
            runner = default_runners(registry).get(_slug)
            if runner is None:
                raise LookupError(f"no runner registered for {_slug!r}")
            from reimagina.db import connect

            # One transaction per unit: the write path is stale-then-insert, and
            # a cancel between the two halves would leave features with no
            # current value at all.
            with connect(os.environ["DATABASE_URL_DIRECT"]) as conn:
                with current_run(input.run_id):
                    runner(
                        conn,
                        features=frozenset(input.features.get(_slug, [])),
                        scenario_id=input.scenario_id,
                        code_version=input.code_version,
                        aoi_id=input.aoi_id,
                    )
                conn.commit()
            return {"slug": _slug}

        node[spec.slug] = workflow.task(
            name=spec.slug,
            parents=parents,
            skip_if=[
                ParentCondition(
                    parent=plan, expression=f"!('{spec.slug}' in output.dirty)"
                )
            ],
            retries=0,  # a runner exception is deterministic; a retry re-burns the minutes
            execution_timeout=UNIT_EXECUTION_TIMEOUT,
            schedule_timeout=UNIT_SCHEDULE_TIMEOUT,
        )(_runner)

    return workflow, node


run_workflow, _nodes = build_dag()
