"""Hatchet ops-plane — the engine owns scheduling and run state.

This module does not implement a scheduler, and it no longer implements a status
model either. It declares the indicator graph once, at import, and Hatchet does
readiness, ordering, fan-out, failure propagation, admission control and
retries. That is the entire reason a service track exists, and it is why
``orchestration/wave.py`` is deleted on this branch.

**The finalizer is eight lines, and that took two corrections to arrive at.**

The first version mapped Hatchet's status enum onto our vocabulary and
apologised in comments for what the enum could not say. The second overcorrected
and rebuilt every per-unit outcome inside a finalizer, from three partial
signals, then string-parsed the engine's display names to label them.

Both were looking in the wrong place. ``runs.get(run_id)`` already reports each
task's status — ``CANCELLED`` included — its error text, and its *declared*
name, so the reading belongs on the read path and needs no hook at all. And
``partial`` needs no storage: it is a roll-up of unit outcomes, computed by
``port.overall_status``, the same function ``sync`` and Track A use.

What is left is the one fact the engine will not hand back: a unit skipped for
being non-dirty reports ``COMPLETED``, identical to one that computed, and
``ctx.was_skipped`` is available only inside a finalizer. So the finalizer emits
the skip list, and nothing else.

The real difference from the library track is not that Track B *cannot say*
``partial``; it is **where run history lives**: Track A keeps it in a table it
owns and can join, aggregate and index with SQL. Track B keeps it in the engine,
reachable one run at a time and gone at its retention horizon. That is a sharper
and more honest comparison row than "the engine is lossy".

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

import logging
import os
import threading
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import psycopg2
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
from pydantic import BaseModel, Field

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.jobs._lifecycle import current_run, record_failed_job_run
from reimagina.orchestration.graph import fingerprint, shape_version, topological_order
from reimagina.orchestration.runners import default_runners
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)

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

# The same bound, expressed where the database can enforce it. Hatchet's
# execution timeout stops scheduling the task and nothing more — the statement
# keeps running and the worker slot stays occupied, because neither engine can
# interrupt a thread inside psycopg2's C extension. `SET LOCAL statement_timeout`
# is the one mechanism that actually ends a runaway PostGIS query.
#
# Derived rather than configured separately: two knobs for one bound drift, and
# the pair that matters is "the engine gave up" against "the database gave up",
# which should happen at the same moment.
UNIT_EXECUTION_TIMEOUT_MS = int(UNIT_EXECUTION_TIMEOUT.total_seconds() * 1000)

# A deterministic runner exception re-burns the minutes on every attempt, so the
# first cut used retries=0. That was a library-track reflex: DBOS made retries
# our problem, so we turned them off. Here the engine distinguishes the cases
# for us — a serialization failure or a dropped connection *is* worth retrying,
# and `NonRetryableException` opts a genuinely deterministic failure out.
UNIT_RETRIES = int(os.environ.get("REIMAGINA_UNIT_RETRIES", "2"))

# The API reads run state but does not need SDK configuration until a request
# actually touches Hatchet. Keeping construction lazy makes pure unit tests and
# worker preflight validation independent of a dashboard token.
#
# ⚠ Two locks, not one, and not zero.
#
# Not zero: `POST /jobs` is a *sync* def, so FastAPI runs it on the anyio
# threadpool. Two concurrent requests against a cold process would each see the
# global as None, each construct a client and each build the DAG — two gRPC
# channels with their background threads, one of them overwritten and leaked.
#
# Not one: `build_dag()` calls `get_hatchet()`, so a single non-reentrant lock
# held across DAG construction would deadlock on that nested acquire. Separate
# locks, always taken in this order (dag -> client), cannot.
_CLIENT_LOCK = threading.Lock()
_DAG_LOCK = threading.Lock()

hatchet: Hatchet | None = None

_dag: tuple[Any, dict[str, Any]] | None = None
"""The declared workflow and its nodes, together.

⚠ One tuple rather than a `run_workflow` global beside a `_nodes` global. Those
two could disagree: `get_run_workflow()` returned early whenever `run_workflow`
was set, without ever assigning `_nodes`, so `nodes()` either tripped its own
assert or handed back nodes belonging to a different graph. A tuple cannot be
half-assigned.
"""


def get_hatchet() -> Hatchet:
    global hatchet
    if hatchet is not None:  # fast path: no lock once constructed
        return hatchet
    with _CLIENT_LOCK:
        if hatchet is None:
            hatchet = Hatchet()
    return hatchet


class RunInput(BaseModel):
    """Everything the run needs, resolved before it is submitted.

    ``dirty`` is derived by the submitter, not by the ``plan`` task, for the
    same reason the library track passes it as a workflow input: the catalog can
    change mid-run, and a graph that re-derived its own work would disagree with
    the one that was scheduled.

    ``concurrency_key`` exists because ``scenario_id`` is nullable — every
    unscoped run would otherwise collapse into one bucket and serialise against
    each other. Computed at submit, and required: see the field.
    """

    run_id: str
    dirty: list[str] = Field(default_factory=list)

    plan_id: str = ""
    """Handle for the resolved feature set, stored in ``jobs_runtime.run_plan``.

    ⚠ This field is why a full-city run can be submitted at all. Carrying the
    ids inline made a CDMX recompute 258,675 uuids — 10,088,804 bytes against
    gRPC's 4,194,304 limit — and the engine refused it before seeing the work.
    A handle is a couple of hundred bytes whatever the size of the city.

    ⚠ Not the engine's run id: that is minted by ``run()`` and unknown here, so
    keying the plan on it would mean writing rows *after* triggering, racing a
    worker that has already dequeued. This id is ours and exists before submit.
    """

    scenario_id: str | None = None
    aoi_id: str | None = None
    code_version: str = "dev"

    graph_version: str = ""
    """Which graph this run was planned against — shape *and* task semantics.

    Not an admission criterion: the workflow *name* carries the shape, so a run
    that reaches a worker at all already agrees on that. This records the rest,
    so that "the API and the worker disagree about a timeout" is a value someone
    can read off a run rather than a theory about a deployment.
    """

    concurrency_key: str
    """The set of rows two runs would fight over. Required — no default.

    ⚠ It defaulted to the constant ``"baseline"``, which is the failure it
    exists to prevent: every run that omitted it landed in one ``max_runs=1``
    bucket and serialised against unrelated work. A default here cannot be
    right, because the correct value is a property of the submitted work, so
    omitting it is always a bug and should fail at construction rather than at
    3am under load.

    Submitters use ``scenario_id or aoi_id or dedup_key``.
    """

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


def _dependencies(registry: Sequence[IndicatorSpec]) -> dict[str, set[str]]:
    """`slug -> prerequisite slugs`, the only thing the graph module needs."""
    return {
        spec.slug: {dependency for dependency, _weight in spec.depends_on}
        for spec in registry
    }


def _topo_order(registry: Sequence[IndicatorSpec]) -> list[IndicatorSpec]:
    """Parents before children for Hatchet's static task declaration."""
    by_slug = {s.slug: s for s in registry}
    return [by_slug[slug] for slug in topological_order(_dependencies(registry))]


WORKFLOW_STEM = "reimagina-run"


def workflow_name(registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS) -> str:
    """`reimagina-run-v<shape>` — the name this deployment's graph answers to.

    ⚠ The version is in the *name*, so an incompatible graph change cannot be
    applied to a workflow that in-flight runs were scheduled against. Old
    workers keep serving the old name until their runs drain, which makes a
    rolling deployment "both names registered for a while" rather than a moment
    where the API plans one shape and the worker executes another.

    Only the shape versions the name. Task semantics — timeouts, retries — ride
    in `graph_version` on the run input, because the engine already versions a
    compatible redefinition under one name and duplicating that here would
    fragment run history on every tuning change.
    """
    return f"{WORKFLOW_STEM}-v{shape_version(_dependencies(registry))}"


def graph_version(registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS) -> str:
    """Shape *plus* task semantics — for observability, not for admission.

    Two deployments agreeing on shape but disagreeing on timeouts produce
    genuinely different runs, and when one is behaving oddly this is the value
    that says so. It is not a rejection criterion: a semantics difference is
    compatible with an in-flight run, and refusing to submit over one would
    make every tuning change an outage.
    """
    return fingerprint(
        shape_version(_dependencies(registry)),
        int(UNIT_EXECUTION_TIMEOUT.total_seconds()),
        int(UNIT_SCHEDULE_TIMEOUT.total_seconds()),
        UNIT_RETRIES,
        _SKIP_EXPRESSION_TEMPLATE,
    )


# Any fixed UUID; it only namespaces the uuid5 derivation below.
_CONDITION_NS = uuid.UUID("6f9b1f2e-2c4a-4d1e-9f3b-8a5c7d2e1b04")

# ⚠ The negation is load-bearing — see `_skip_unless_dirty`. Named here rather
# than inlined so `graph_version` hashes the actual expression: a change to
# *what makes a unit skip* is a semantics change worth recording, and one
# spelled in a format string is easy to alter without noticing.
_SKIP_EXPRESSION_TEMPLATE = "!('{slug}' in output.dirty)"


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
        expression=_SKIP_EXPRESSION_TEMPLATE.format(slug=slug),
        readable_data_key=f"dirty:{slug}",
    )
    condition.base.or_group_id = str(uuid.uuid5(_CONDITION_NS, f"dirty:{slug}"))
    return [condition]


def build_dag(
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
    *,
    name: str | None = None,
    client: Hatchet | None = None,
):
    """Declare one task per indicator. This is the whole scheduler.

    ``name`` exists so a caller can declare a *second*, differently-shaped graph
    without touching this one. Two different DAGs under one name is engine-side
    behaviour we have not established (spike B9) — whether it versions,
    overwrites or rejects — so anything that needs its own topology takes its own
    name rather than finding out. The workload suite uses ``reimagina-bench-*``.
    """
    # Resolved once, before anything is constructed, rather than per task
    # attempt. A missing runner is a deployment error: failing here means the
    # worker refuses to advertise a workflow it cannot execute, instead of
    # accepting the work and failing each unit `UNIT_RETRIES + 1` times with
    # backoff in between. Before the client, so the check does not need one.
    runners = default_runners(registry)
    missing = sorted(spec.slug for spec in registry if spec.slug not in runners)
    if missing:
        raise ValueError(f"no runner registered for indicator(s): {missing!r}")

    workflow = (client or get_hatchet()).workflow(
        name=name or workflow_name(registry),
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
        # ⚠ Do not use CANCEL_IN_PROGRESS: cancellation here is cooperative and
        # nothing interrupts a blocking
        # psycopg call (spike B4), so CANCEL_IN_PROGRESS would start the new run
        # while the old one is still writing — exactly the two-writer case.
        #
        # A newer edit must not be dropped. Group-round-robin keeps work for the
        # same write scope serial while allowing independent scopes to progress.
        concurrency=ConcurrencyExpression(
            expression="input.concurrency_key",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
        ),
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

        def _runner(
            input: RunInput,
            ctx: Context,
            _slug: str = spec.slug,
            runner=runners[spec.slug],
        ) -> dict:
            from reimagina.db import connect

            # Cancellation is cooperative, and this is the one place it is
            # cheap: a unit cancelled while it sat in the queue has not opened a
            # transaction yet, so declining to start costs nothing and saves a
            # `pg_cancel_backend` round trip against a backend that would
            # otherwise have to be hunted down. It does nothing for a unit
            # already inside a blocking psycopg2 call — see the runbook.
            #
            # ⚠ `is_cancelled`, not `exit_flag`. The SDK marks `exit_flag`
            # internal and warns on every read: "It will be removed in v2.0.0.
            # Use `is_cancelled` instead." Reaching for a deprecated internal to
            # read cancellation state is the same mistake as reaching into
            # `_action_listener_process` to stop a worker.
            if ctx.is_cancelled:
                raise NonRetryableException(f"{_slug}: cancelled before it started")

            # ⚠ `ctx.workflow_run_id`, NOT `input.run_id`. The submitter cannot
            # know the id — the engine mints it — so `RunInput.run_id` is an
            # empty-string placeholder. Reading it here passed `""` into
            # `job_run.run_id uuid` and every unit died on
            # `invalid input syntax for type uuid: ""`. The DAG had never once
            # run end to end; the conformance suite is what caught it, because
            # nothing before it executed a real unit against a real database.
            direct_dsn = os.environ["DATABASE_URL_DIRECT"]
            conn = connect(direct_dsn)
            try:
                # The last bound, and the only one the database itself enforces.
                # Hatchet's `execution_timeout` stops *scheduling* the task; it
                # cannot interrupt a statement, and neither can a worker thread
                # blocked in psycopg2's C extension. `statement_timeout` is what
                # actually ends a runaway PostGIS query.
                #
                # `SET LOCAL` scopes it to this transaction, so it cannot leak
                # onto a pooled connection — this connection is per-unit anyway,
                # but the local form is the one that stays correct if that
                # changes. It is the first statement, which also opens the
                # implicit transaction it is local to.
                #
                # ⚠ Raises `QueryCanceled`, which `_is_retryable` classifies
                # non-retryable — deliberately. This is a final bound, not a
                # transient fault; retrying it burns another full timeout.
                with conn.cursor() as cur:
                    cur.execute(
                        "SET LOCAL statement_timeout = %s",
                        (UNIT_EXECUTION_TIMEOUT_MS,),
                    )
                # ⚠ Read *after* the connection is open, not from the payload.
                # The feature ids live in `jobs_runtime.run_plan` under
                # `input.plan_id`; carrying them in the workflow input made a
                # full-city run a 10 MB gRPC message the engine refused outright.
                #
                # `features_for` raises `LookupError` for an absent plan rather
                # than returning empty — a unit computing zero features would
                # report success and leave the indicator stale with nothing to
                # attribute it to.
                features = frozenset(
                    backend_for_connection(conn)
                    .run_plan_repo()
                    .features_for(plan_id=input.plan_id, indicator_slug=_slug)
                )
                # Free in the dashboard's log viewer, per task, searchable. The
                # library track has nowhere to put this but the process log.
                ctx.log(
                    f"{_slug}: {len(features)} feature(s), attempt {ctx.attempt_number}"
                )
                with current_run(ctx.workflow_run_id):
                    runner(
                        conn,
                        features=features,
                        scenario_id=input.scenario_id,
                        code_version=input.code_version,
                        aoi_id=input.aoi_id,
                    )
                conn.commit()
            except Exception as exc:
                # ⚠ `Exception`, never `BaseException`. The wider clause caught
                # KeyboardInterrupt, SystemExit and CancelledError and re-raised
                # them as `NonRetryableException` — which subclasses `Exception`
                # — so a worker being shut down recorded a healthy unit as a
                # permanent failure. A signal is not a unit outcome; let it go.
                # `finally: conn.close()` still runs either way.
                conn.rollback()
                # That rollback just erased the `failed` job_run the runner
                # opened — it was written on this connection. Re-record it on
                # its own, or a unit that burned minutes and failed leaves no
                # provenance at all. Once per attempt, deliberately: each retry
                # really is a separate failed execution.
                #
                # ⚠ `direct_dsn`, NOT `conn.dsn`. psycopg2 returns a live
                # connection's DSN with the password replaced by the literal
                # `xxx`, so reconnecting from it fails authentication against
                # any database that has one — and the best-effort `except`
                # inside swallows that into a log line, so failed units simply
                # stop leaving provenance. The string this connection was
                # opened with is the same database by construction, and works.
                record_failed_job_run(
                    direct_dsn,
                    run_id=ctx.workflow_run_id,
                    indicator_slug=_slug,
                    scenario_id=input.scenario_id,
                    code_version=input.code_version,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if _is_retryable(exc):
                    raise
                raise NonRetryableException(f"{type(exc).__name__}: {exc}") from exc
            finally:
                # The runner owns this direct connection for the unit's entire
                # transaction, so it must always close it before the task exits.
                conn.close()
            return {"slug": _slug, "features": len(features)}

        node[spec.slug] = workflow.task(
            name=spec.slug,
            parents=parents,
            skip_if=_skip_unless_dirty(plan, spec.slug),
        )(_runner)

    # ── The one fact the engine will not report: which units were skipped ───
    #
    # `units` is captured by both finalizers. Note it deliberately excludes
    # `plan`: scaffolding is not an indicator, and a caller asking "what
    # happened to my run" does not want a fourth row for our bookkeeping.
    units = {slug: task for slug, task in node.items() if slug != "plan"}

    def _skipped(ctx: Context) -> dict:
        """Record which units were skipped. That is the whole job.

        Everything else a caller wants about a unit is already in
        `runs.get(run_id)`: `tasks[].status` distinguishes COMPLETED, FAILED and
        CANCELLED, `tasks[].error_message` carries the failure text, and
        `shape[].task_name` gives the declared slug. Reconstructing any of that
        here meant maintaining a second, worse copy of the engine's own record —
        and it was wrong twice, because inferring "cancelled" from a missing
        output is a three-signal guess where the engine has a plain answer.

        ⚠ What is *not* recoverable afterwards is a skip. A unit skipped for
        being non-dirty reports **COMPLETED**, indistinguishable from one that
        computed, and `ctx.was_skipped` is the documented way to tell them apart
        — available only inside a finalizer, which is why this one still exists.
        """
        return {
            "skipped": sorted(
                slug for slug, task in units.items() if ctx.was_skipped(task)
            )
        }

    # ⚠ Neither finalizer deletes the plan. A terminal run is exactly the run a
    # native replay reuses, and replay re-reads `run_plan` by the same handle —
    # reclaiming it here destroyed the input of every replayable run. Retention
    # is a Postgres-owned operation outside the request path; see the ops job.
    #
    # Both hooks, one body: Hatchet fires `on_failure_task` only when a run did
    # not fully succeed, so neither alone covers every run.
    @workflow.on_success_task()
    def summarise_success(input: RunInput, ctx: Context) -> dict:
        return _skipped(ctx)

    @workflow.on_failure_task()
    def summarise_failure(input: RunInput, ctx: Context) -> dict:
        """Runs when the run did not fully succeed — including partial success."""
        return _skipped(ctx)

    return workflow, node


def _get_dag() -> tuple[Any, dict[str, Any]]:
    """Declare the production DAG once, atomically.

    ``_dag`` stays replaceable for the workload suite, whose distinct workflow
    names intentionally exercise alternate static DAG shapes — but it is
    replaced as a *pair*, so the workflow and the nodes describing it can never
    come from different builds.
    """
    global _dag
    if _dag is not None:  # fast path: no lock once built
        return _dag
    with _DAG_LOCK:
        if _dag is None:
            _dag = build_dag()
    return _dag


def get_run_workflow() -> Any:
    return _get_dag()[0]


def nodes() -> dict[str, Any]:
    return _get_dag()[1]


def _is_retryable(exc: BaseException) -> bool:
    """Return whether retrying a rolled-back unit can plausibly make progress.

    ⚠ The cancellation check must come first, because `QueryCanceled` inherits
    from `OperationalError` and the broad clause below would otherwise claim it.
    `cancel_run` stops a unit with `pg_cancel_backend`, so classifying that as
    retryable hands the engine a cancelled unit to run again — up to
    `REIMAGINA_UNIT_RETRIES` more times, each free to burn the full
    `REIMAGINA_UNIT_TIMEOUT_MIN`. The same exception arrives from a role-level
    `statement_timeout`, where retrying is equally pointless.
    """
    if isinstance(exc, psycopg2.errors.QueryCanceled):
        return False
    if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        return True
    code = getattr(exc, "pgcode", None)
    return code in {"40001", "40P01"}  # serialization failure, deadlock


# ⚠ No `_produced_output`. It inferred "the engine cancelled this unit" from
# `ctx.task_output` raising — a three-signal guess (no error, not skipped, no
# output) standing in for something the engine states outright:
# `V1TaskSummary.status == CANCELLED`, read from `runs.get(run_id).tasks`. The
# guess was wrong once already, classifying a cancelled dependent as succeeded
# and presenting a value that was never computed as a good one.
