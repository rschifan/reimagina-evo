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
from reimagina.jobs._lifecycle import current_run, record_failed_job_run
from reimagina.orchestration.graph import topological_order
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

    dedup_key: str = ""
    """Fingerprint of the *work*, for the idempotency window.

    ⚠ Must cover the dirty set and the feature scope, not just the scenario.
    Keying on ``scenario_id + code_version`` — the obvious choice, and the one
    written first — deduplicates two *different* edits to one scenario inside
    the TTL, so the second edit's changes are silently never computed. That is
    data loss wearing a dedup's clothes. Caught by the conformance suite, which
    submits repeatedly with identical parameters and started failing the moment
    idempotency was declared.

    Computed by the submitter because the workflow cannot: CEL has no hashing,
    and a ``key_expression`` over a list field cannot express "these are the
    same units with the same features"."""


def _topo_order(registry: Sequence[IndicatorSpec]) -> list[IndicatorSpec]:
    """Parents before children for Hatchet's static task declaration."""
    dependencies = {
        spec.slug: {dependency for dependency, _weight in spec.depends_on}
        for spec in registry
    }
    by_slug = {s.slug: s for s in registry}
    return [by_slug[slug] for slug in topological_order(dependencies)]


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


def build_dag(
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
    *,
    name: str = "reimagina-run",
):
    """Declare one task per indicator. This is the whole scheduler.

    ``name`` exists so a caller can declare a *second*, differently-shaped graph
    without touching this one. Two different DAGs under one name is engine-side
    behaviour we have not established (spike B9) — whether it versions,
    overwrites or rejects — so anything that needs its own topology takes its own
    name rather than finding out. The workload suite uses ``reimagina-bench-*``.
    """
    workflow = hatchet.workflow(
        name=name,
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
            key_expression="input.dedup_key",
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

            # ⚠ `ctx.workflow_run_id`, NOT `input.run_id`. The submitter cannot
            # know the id — the engine mints it — so `RunInput.run_id` is an
            # empty-string placeholder. Reading it here passed `""` into
            # `job_run.run_id uuid` and every unit died on
            # `invalid input syntax for type uuid: ""`. The DAG had never once
            # run end to end; the conformance suite is what caught it, because
            # nothing before it executed a real unit against a real database.
            conn = connect(os.environ["DATABASE_URL_DIRECT"])
            try:
                with current_run(ctx.workflow_run_id):
                    runner(
                        conn,
                        features=features,
                        scenario_id=input.scenario_id,
                        code_version=input.code_version,
                        aoi_id=input.aoi_id,
                    )
                conn.commit()
            except BaseException as exc:
                conn.rollback()
                # That rollback just erased the `failed` job_run the runner
                # opened — it was written on this connection. Re-record it on
                # its own, or a unit that burned minutes and failed leaves no
                # provenance at all. Once per attempt, deliberately: each retry
                # really is a separate failed execution.
                record_failed_job_run(
                    conn.dsn,
                    run_id=ctx.workflow_run_id,
                    indicator_slug=_slug,
                    scenario_id=input.scenario_id,
                    code_version=input.code_version,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            finally:
                # `with connect(...)` scopes the transaction, not the socket,
                # so the previous shape leaked a connection per unit per run.
                conn.close()
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

        Four sources, none of which is the status enum. The order matters, and
        the third check is the one that is easy to get wrong — measured in
        spike B10:

        | unit was              | error | was_skipped | task_output       |
        |-----------------------|-------|-------------|-------------------|
        | computed              | –     | False       | the value         |
        | failed                | set   | False       | raises *not found*|
        | **cancelled** (parent failed) | – | **False** | raises *not found*|
        | skipped (not dirty)   | –     | **True**    | raises *was skipped*|

        ⚠ A cancelled dependent carries **no error and `was_skipped=False`**, so
        classifying on those two alone reports a composite that never ran as
        `succeeded` — a wrong value presented as a good one, which is worse than
        the vocabulary loss this design exists to fix. Absence of an output is
        what separates it from a real success.
        """
        errors = ctx.task_run_errors
        units_out: dict[str, dict[str, str | None]] = {}
        for slug, task in units.items():
            error = errors.get(slug)
            if error is not None:
                status = "failed"
            elif ctx.was_skipped(task):
                status = "skipped"
            elif not _produced_output(ctx, task):
                # Never ran, and not because it was skipped: the engine
                # cancelled it when a parent failed (spike B3). `cancelled`
                # rather than `skipped` because that is what happened — the
                # library track's `skipped` says *why*, and this does not.
                status = "cancelled"
            else:
                status = "succeeded"
            units_out[slug] = {
                "status": status,
                "detail": str(error)[:500] if error is not None else None,
            }

        seen = {u["status"] for u in units_out.values()}
        if "failed" not in seen:
            overall = "succeeded"
        elif "succeeded" in seen:
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


def _produced_output(ctx: Context, task: Any) -> bool:
    """Did this task return a value?

    ``ctx.task_output`` raises ``ValueError`` when there is nothing to return —
    both for a task that failed and for one the engine cancelled before it ran
    (spike B10). Broad except on purpose: the SDK gives no typed "no output"
    signal, and any failure to read an output means we cannot claim the unit
    succeeded.
    """
    try:
        ctx.task_output(task)
    except Exception:  # noqa: BLE001
        return False
    return True


run_workflow, _nodes = build_dag()
