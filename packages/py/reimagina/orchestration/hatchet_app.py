"""Hatchet ops-plane — the engine owns scheduling, run state *and* the verdict.

This module does not implement a scheduler, and it no longer implements a status
model either. It declares the indicator graph once, at import, and Hatchet does
readiness, ordering, fan-out, failure propagation, admission control and
retries. That is the entire reason a service track exists, and it is why
``orchestration/wave.py`` is deleted on this branch.

**What changed from the first cut of this module**, and why it matters to the
comparison: the first version mapped Hatchet's status enum onto our vocabulary
and apologised in comments for what the enum could not say. That was the wrong
place to look. Hatchet's *enum* has no ``partial`` and no ``skipped`` — true,
measured — but a run's *output* is arbitrary JSON, and ``on_failure_task`` is a
shipped hook that runs precisely when some units succeeded and one did not.

Spike B8 confirmed the finalizer runs on a half-failed run and reads per-unit
outcomes, returning::

    {"overall": "partial",
     "verdict": {"clean_unit": {"skipped": true},
                 "ok_unit":    {"skipped": false},
                 "doomed":     {"error": "RuntimeError: boom..."}}}

So the "accepted fidelity loss" was mostly self-inflicted. The real difference
from the library track is not that Track B *cannot say* ``partial``; it is
**where the vocabulary lives**: Track A keeps it in a table it owns and can
join, aggregate and index with SQL. Track B keeps it in the run's output,
reachable only by asking the engine one run at a time. That is a sharper and
more honest comparison row than "the engine is lossy".

**Behaviours inherited rather than implemented** (all measured, see
``docs/product/PHASE3_OPS_PLANE_HATCHET_SPIKES.md``):

* a dirty composite whose *non-dirty* input was skipped still runs (B2);
* a failed unit cancels its dependents while independent branches finish, and
  the run stays alive until they do (B3);
* run-level dedup and scenario supersession — declared, not coded (B8);
* per-task logs, OTel trace context, DAG visualisation and one-click replay,
  none of which we write.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
    Hatchet,
    NonRetryableException,
    ParentCondition,
    TaskDefaults,
    TTLBasedIdempotencyConfig,
)
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
#
# ponytail: a fixed ceiling, not a heartbeat. `ctx.refresh_timeout()` exists and
# would let a unit extend its own deadline as it progresses — strictly better,
# because a wedged unit would then die in minutes instead of burning the full
# two hours. It needs a progress callback the runner interface does not have
# (`runners.default_runners` takes no callback), so it is the documented upgrade
# path rather than something faked with a watchdog thread. A watchdog that
# refreshed unconditionally would tick just as happily for a wedged unit, which
# is worse than the ceiling it replaced.
UNIT_EXECUTION_TIMEOUT = timedelta(
    minutes=int(os.environ.get("REIMAGINA_UNIT_TIMEOUT_MIN", "120"))
)
UNIT_SCHEDULE_TIMEOUT = timedelta(
    hours=int(os.environ.get("REIMAGINA_QUEUE_TIMEOUT_HOURS", "6"))
)

# A deterministic runner exception re-burns the minutes on every attempt, so the
# first cut used retries=0. That was a library-track reflex: DBOS made retries
# our problem, so we turned them off. Here the engine distinguishes the cases
# for us — a serialization failure or a dropped connection *is* worth retrying,
# and `NonRetryableException` opts a genuinely deterministic failure out.
UNIT_RETRIES = int(os.environ.get("REIMAGINA_UNIT_RETRIES", "2"))

hatchet = Hatchet()


class RunInput(BaseModel):
    """Everything the run needs, resolved before it is submitted.

    ``dirty`` is derived by the submitter, not by the ``plan`` task, for the
    same reason the library track passes it as a workflow input: the catalog can
    change mid-run, and a graph that re-derived its own work would disagree with
    the one that was scheduled.

    ``concurrency_key`` exists because ``scenario_id`` is nullable — every
    baseline run would otherwise collapse into one bucket and serialise against
    each other. Computed at submit.
    """

    run_id: str
    dirty: list[str] = []
    features: dict[str, list[str]] = {}
    scenario_id: str | None = None
    aoi_id: str | None = None
    code_version: str = "dev"
    concurrency_key: str = "baseline"


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


# Any fixed UUID; it only namespaces the uuid5 derivation below.
_CONDITION_NS = uuid.UUID("6f9b1f2e-2c4a-4d1e-9f3b-8a5c7d2e1b04")


def _skip_unless_dirty(plan: Any, slug: str) -> list[ParentCondition]:
    """Skip this node when its slug is absent from the dirty set.

    ``skip_if`` means *skip when the expression is true* — measured in spike B1,
    where the un-negated form ran exactly the indicators it should have left
    alone while every task reported COMPLETED. The negation is load-bearing.

    ⚠ The two overrides below are a bug fix, not tidiness. Left to its defaults,
    ``ParentCondition`` makes the DAG declaration **non-deterministic across
    processes**, from two independent sources
    (``hatchet_sdk/conditions.py:41-44,118-128``):

      * ``readable_data_key`` defaults to
        ``parent.name + ":" + expression + ":" + datetime.now(UTC).isoformat()``
        — a microsecond timestamp that reaches the wire;
      * ``or_group_id`` defaults to ``Field(default_factory=uuid4)`` — a fresh
        random UUID per condition per build.

    Verified in spike B9: two ``build_dag()`` calls in one process serialise to
    different bytes, differing in exactly these fields. The API and the worker
    both build this graph at import, so without the overrides they describe the
    same workflow differently every time either one starts. Each of our
    conditions is alone in its OR group, so any stable per-condition value
    works; ``uuid5`` keeps it both deterministic and a well-formed UUID.
    """
    condition = ParentCondition(
        parent=plan,
        expression=f"!('{slug}' in output.dirty)",
        readable_data_key=f"dirty:{slug}",
    )
    condition.base.or_group_id = str(uuid.uuid5(_CONDITION_NS, f"dirty:{slug}"))
    return [condition]


def build_dag(registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS):
    """Declare one task per indicator. This is the whole scheduler."""
    workflow = hatchet.workflow(
        name="reimagina-run",
        input_validator=RunInput,
        # Set once, not threaded through every task() call in the loop below.
        task_defaults=TaskDefaults(
            execution_timeout=UNIT_EXECUTION_TIMEOUT,
            schedule_timeout=UNIT_SCHEDULE_TIMEOUT,
            retries=UNIT_RETRIES,
            backoff_factor=2.0,
            backoff_max_seconds=60,
        ),
        # ── Admission control, declared rather than coded ────────────────────
        #
        # The plan records run-level dedup as "never implemented" on any rung:
        # `submit_run` mints a fresh uuid4() per call, so a double-clicked Save
        # starts two identical multi-minute runs. Here it is a config field.
        # Short TTL on purpose — this is a debounce, not a result cache.
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.concurrency_key + ':' + input.code_version",
            ttl=timedelta(seconds=int(os.environ.get("REIMAGINA_DEDUP_TTL_S", "60"))),
        ),
        # Two runs writing the same scenario's rows concurrently is the hazard
        # that matters: the write path is stale-then-insert, so overlapping
        # writers interleave and leave features with the wrong current value.
        #
        # ⚠ CANCEL_NEWEST, not CANCEL_IN_PROGRESS, and the difference is safety:
        # cancellation here is cooperative and nothing interrupts a blocking
        # psycopg call (spike B4), so CANCEL_IN_PROGRESS would start the new run
        # while the old one is still writing — exactly the two-writer case.
        #
        # ⚠ The cost is real and is a product decision, not an implementation
        # detail: a second edit arriving mid-run is *dropped*, not queued. The
        # user must resubmit. Hatchet's enum offers no "queue behind" strategy.
        concurrency=ConcurrencyExpression(
            expression="input.concurrency_key",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
        # A scenario edit anywhere — a DB trigger, another service, a backfill
        # script — can start a run without importing us or calling our API.
        # Track A has no equivalent: DBOS has no event bus, so every submitter
        # must be a Python process that imports the workflow definitions.
        on_events=["scenario:edited"],
        # Nightly baseline recompute. One argument against a scheduler.
        on_crons=[os.environ["REIMAGINA_RECOMPUTE_CRON"]]
        if os.environ.get("REIMAGINA_RECOMPUTE_CRON")
        else None,
    )

    @workflow.task(execution_timeout=timedelta(minutes=5), retries=1)
    def plan(input: RunInput, ctx: Context) -> dict:
        """Publish the dirty set for the graph to read. Writes nothing.

        Returns slugs only — every child receives every parent's output, and
        `plan` parents every node, so this is the payload that scales. Slugs
        keep it at 72 KB even for a 2,000-indicator catalog (spike B6).
        """
        ctx.log(f"planning {len(input.dirty)} unit(s): {', '.join(input.dirty)}")
        return {"dirty": input.dirty}

    node: dict[str, Any] = {"plan": plan}

    for spec in _topo_order(registry):
        parents = [plan] + [node[dep] for dep, _w in spec.depends_on if dep in node]

        def _runner(input: RunInput, ctx: Context, _slug: str = spec.slug) -> dict:
            runner = default_runners(registry).get(_slug)
            if runner is None:
                # A missing runner is a deployment error, not a transient one.
                # Retrying it three times with backoff just delays the report.
                raise NonRetryableException(f"no runner registered for {_slug!r}")
            from reimagina.db import connect

            features = frozenset(input.features.get(_slug, []))
            # Free in the dashboard's log viewer, per task, searchable. The
            # library track has nowhere to put this but the process log.
            ctx.log(f"{_slug}: {len(features)} feature(s), attempt {ctx.attempt_number}")

            # One transaction per unit: the write path is stale-then-insert, and
            # a cancel between the two halves would leave features with no
            # current value at all.
            with connect(os.environ["DATABASE_URL_DIRECT"]) as conn:
                with current_run(input.run_id):
                    runner(
                        conn,
                        features=features,
                        scenario_id=input.scenario_id,
                        code_version=input.code_version,
                        aoi_id=input.aoi_id,
                    )
                conn.commit()
            return {"slug": _slug, "features": len(features)}

        node[spec.slug] = workflow.task(
            name=spec.slug,
            parents=parents,
            skip_if=_skip_unless_dirty(plan, spec.slug),
        )(_runner)

    # ── The verdict, which is where the run vocabulary actually lives ────────
    #
    # `units` is captured by both finalizers. Note it deliberately excludes
    # `plan`: scaffolding is not an indicator, and a caller asking "what
    # happened to my run" does not want a fourth row for our bookkeeping.
    units = {slug: task for slug, task in node.items() if slug != "plan"}

    def _verdict(ctx: Context) -> dict:
        """Reconstruct per-unit outcomes from what the engine already knows.

        Three sources, none of which is the status enum:
          * ``ctx.was_skipped(task)`` — the ``{"skipped": true}`` output
            sentinel, which is what separates "not dirty" from "computed".
          * ``ctx.task_run_errors`` — keyed by task name, holds the traceback.
          * absence of both — the unit succeeded.
        """
        errors = ctx.task_run_errors
        units_out: dict[str, dict[str, str | None]] = {}
        for slug, task in units.items():
            error = errors.get(slug)
            if error is not None:
                status = "failed"
            elif ctx.was_skipped(task):
                status = "skipped"
            else:
                status = "succeeded"
            units_out[slug] = {
                "status": status,
                "detail": str(error)[:500] if error is not None else None,
            }

        seen = {u["status"] for u in units_out.values()}
        if "failed" not in seen:
            overall = "succeeded"
        elif seen & {"succeeded"}:
            overall = "partial"  # ⚠ the word the status enum cannot say
        else:
            overall = "failed"
        return {"overall": overall, "units": units_out}

    @workflow.on_success_task()
    def summarise_success(input: RunInput, ctx: Context) -> dict:
        return _verdict(ctx)

    @workflow.on_failure_task()
    def summarise_failure(input: RunInput, ctx: Context) -> dict:
        """Runs when the run did not fully succeed — including partial success.

        Verified in spike B8: fires with one unit failed and two not, reports
        COMPLETED itself, and its return value is what ``runs.get_result()``
        serves for a run whose *status* is FAILED.
        """
        return _verdict(ctx)

    return workflow, node


run_workflow, _nodes = build_dag()
