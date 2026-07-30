"""The Orchestrator port over Hatchet.

The engine owns run state, so ``get_run`` reads no table of ours — and, since
the second correction to this module, reconstructs almost nothing either.

``runs.get(run_id)`` returns three collections, and for a long time this code
used one of them. ``shape`` names the tasks as declared; ``tasks`` carries each
one's status — including ``CANCELLED`` — plus its error text; they join on
``task_external_id``. Every per-unit outcome is therefore *stated* by the
engine. The version before this one rebuilt them instead, from
``ctx.task_run_errors`` plus ``ctx.was_skipped`` plus whether ``task_output``
raised, inside a finalizer, and then parsed display names to undo the engine's
own suffixing. It was a second, worse copy of a record the engine already keeps,
and it was wrong twice — once reporting a cancelled dependent as ``succeeded``.

What genuinely cannot be read back is a **skip**: a unit skipped for being
non-dirty reports ``COMPLETED``, and ``ctx.was_skipped`` exists only inside a
finalizer. So one finalizer survives, emitting the skip list and nothing else.

``partial`` needs no storage at all. It is a roll-up of unit outcomes, computed
on read by ``port.overall_status`` — the same function ``sync`` and Track A use,
so all three agree by construction rather than by review.

What actually differs from the library track is **where run history lives**:

    Track A  our table    — joinable, indexable, aggregable with SQL
    Track B  the engine   — one run at a time, and gone at its retention horizon

"How many scenarios partially failed this week" is a ``GROUP BY`` on one track
and N+1 engine round-trips on the other. That is the comparison row, and it is a
sharper one than "the engine is lossy" because it survives contact with the
evidence.

**The run id is Hatchet's workflow run id.** There is no ``orchestration_run``
table on this branch to hold a mapping, and inventing one would be re-adopting
the state model this track exists to give away.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from hatchet_sdk.exceptions import IdempotencyCollisionError

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.plan import noop_handle, plan_run
from reimagina.orchestration.database_identity import assert_same_domain_database
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    RunEvent,
    RunHandle,
    RunStatus,
    UnitStatusView,
    overall_status,
)
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)


def _persist_plan(direct_dsn: str, units: Sequence[object]) -> str:
    """Store the resolved feature set; return the handle the engine will carry.

    ⚠ Commits and closes before returning, and that ordering is the whole point.
    A worker can dequeue the instant ``run()`` is called, and it reads the plan
    on its own connection — an uncommitted write is indistinguishable from a
    lost one, so the run would fail loudly having been perfectly valid. Owning
    the transaction here makes "commit before trigger" structural rather than a
    comment someone has to honour.

    ⚠ Its own connection, not the caller's. ``submit_run`` borrows a connection
    from the API request, and committing that would end a transaction this layer
    does not own. ``record_failed_job_run`` opens its own for the same reason.
    ``assert_same_domain_database`` has already established that ``direct_dsn``
    and the caller's connection are the same database, so the plan lands where
    the planner looked.

    ⚠ Not placed in ``orchestration/plan.py`` despite being engine-neutral: that
    module states it "touches no queue, worker or run table", and this writes to
    ``jobs_runtime``. Glue for one caller lives beside that caller.

    ⚠ No sweep here. Reclaiming by age on the submit path deleted live input:
    the horizon was 24 h while the engine retains runs for 720 h, so anything
    replayed after a day lost its plan. Retention is now a Postgres-owned
    operation outside the request path, with a horizon that must exceed engine
    retention. Only the two provably-unreferenced cases below delete a plan.
    """
    from reimagina.db import connect

    plan_id = str(uuid4())
    with closing(connect(direct_dsn)) as plan_conn:
        backend_for_connection(plan_conn).run_plan_repo().write(
            plan_id=plan_id,
            units={u.indicator: sorted(u.features) for u in units},
        )
        plan_conn.commit()
    return plan_id


def _discard_plan(direct_dsn: str, plan_id: str) -> None:
    """Delete a plan that no run will ever read.

    ⚠ Only for the two cases where that is *provable*: an idempotency collision
    (the accepted run carries a different ``plan_id``) and a trigger that
    definitively never reached the engine. An ambiguous failure — a deadline or
    an unavailable engine — must **retain**, because the run may have been
    accepted and a retry will deduplicate onto it. Retaining costs one dead row
    for one retention horizon; deleting costs a live run every unit it has left.
    """
    from reimagina.db import connect

    try:
        with closing(connect(direct_dsn)) as conn:
            backend_for_connection(conn).run_plan_repo().delete(plan_id=plan_id)
            conn.commit()
    except Exception:  # noqa: BLE001 — the submit outcome is already decided
        logger.warning("could not discard unused run plan %s", plan_id, exc_info=True)


# gRPC statuses that mean the engine refused the submission outright, before it
# could have created a run. Everything else — DEADLINE_EXCEEDED, UNAVAILABLE,
# INTERNAL, UNKNOWN, ABORTED — is ambiguous and must retain the plan.
_NOT_ACCEPTED_CODES = frozenset(
    {
        "INVALID_ARGUMENT",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "NOT_FOUND",
    }
)


def _is_definitely_not_accepted(exc: BaseException) -> bool:
    """True only when the engine provably never took the run.

    Defaults to ``False``, and the asymmetry is the point: a wrong ``True``
    deletes the input of a live run and every remaining unit fails with a
    missing-plan error; a wrong ``False`` leaves one row until the retention job
    runs. Duck-typed on ``.code()`` rather than importing grpc — the SDK wraps
    its own transport, and an exception without a readable status is exactly the
    ambiguous case this must not act on.
    """
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            code = code()
        except Exception:  # noqa: BLE001 — unreadable status is ambiguous
            return False
    return getattr(code, "name", None) in _NOT_ACCEPTED_CODES


class HatchetUnavailable(RuntimeError):
    """Hatchet could not answer a request for run state."""


class GraphVersionUnavailable(RuntimeError):
    """Nothing has ever deployed the graph this deployment plans against.

    ⚠ Deliberately not a `HatchetUnavailable`. The engine is reachable and
    healthy; what is missing is a worker for *this* workflow name, which is a
    deployment mismatch — a rollout half-applied, or an API ahead of its
    workers. Reporting it as an outage would send someone to look at the engine,
    and would make it retryable, which it is not: retrying cannot conjure a
    worker for a graph nobody deployed.
    """


def _graph_is_deployed(workflow: str) -> bool:
    """Has this workflow name ever been registered with the engine?

    ⚠ Registration, **not** current worker liveness, and the distinction cost a
    conformance run to learn. Gating submissions on a live ACTIVE worker looked
    stricter and was simply wrong: worker status moves moment to moment, so a
    healthy deployment with a busy or briefly-restarting worker refused runs it
    should have accepted — and a rejected submission discards real work, where a
    momentary lack of a worker only delays it. The engine queues.

    A workflow name exists only because a worker declared it, so its absence is
    the condition actually worth refusing: this deployment plans against a graph
    shape nobody has ever deployed, and no amount of queueing fixes that.

    Liveness still matters, but it belongs in **readiness**, where failing takes
    the instance out of rotation instead of destroying a request's work.

    Fails **open** on an unreadable listing: an admission check must not become
    a submission outage.
    """
    from reimagina.orchestration import hatchet_app

    try:
        listed = hatchet_app.get_hatchet().workflows.list()
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not list workflows; admitting run unchecked", exc_info=True
        )
        return True

    names = {getattr(w, "name", None) for w in (getattr(listed, "rows", None) or [])}
    return workflow in names


def _live_worker_for(workflow: str) -> bool:
    """Is a worker *right now* able to execute `workflow`?

    Worker actions are `<workflow-name>:<task-name>`, so the prefix is the
    question, and an INACTIVE registration is history rather than capacity.

    Used by readiness, never by submission — see `_graph_is_deployed`. Fails
    **closed** by returning False on an unreadable listing, because a probe that
    cannot tell should take the instance out of rotation rather than claim it is
    fine.
    """
    from reimagina.orchestration import hatchet_app

    try:
        rows = getattr(hatchet_app.get_hatchet().workers.list(), "rows", None) or []
    except Exception:  # noqa: BLE001
        logger.warning("could not list workers for readiness", exc_info=True)
        return False

    prefix = f"{workflow}:"
    return any(
        str(getattr(row, "status", "")).rsplit(".", 1)[-1] == "ACTIVE"
        and any(
            str(a).startswith(prefix) for a in (getattr(row, "actions", None) or [])
        )
        for row in rows
    )


# Used while the run is still open. Once it is terminal the unit outcomes are
# rolled up through `port.overall_status` instead, which is the only way to
# reach `partial` — the enum has no such value and never will.
_RUN_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETED": "succeeded",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}

# The whole adapter between the engine's task enum and the port's unit
# vocabulary. Five entries, one direction, no inference.
_UNIT_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    # ⚠ COMPLETED covers both "computed" and "skipped because not dirty" — the
    # engine draws no distinction. The finalizer's skip list is overlaid on top
    # of this in `_units_of`; until it exists, a skipped unit reads as succeeded.
    "COMPLETED": "succeeded",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}


class HatchetOrchestrator:
    """Scheduling and run state belong to the engine; this adapts them."""

    def __init__(
        self, *, registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS
    ) -> None:
        self._registry = registry

    # ── Submit ──────────────────────────────────────────────────────────────

    def submit_run(
        self,
        conn: object,
        *,
        changed_attributes: Iterable[str],
        edited_features: Iterable[str],
        scenario_id: str | None = None,
        aoi_id: str | None = None,
        code_version: str = "dev",
    ) -> RunHandle:
        from reimagina.orchestration import hatchet_app

        direct_dsn = os.environ.get("DATABASE_URL_DIRECT")
        if not direct_dsn:
            raise RuntimeError("DATABASE_URL_DIRECT is required for Hatchet submission")
        assert_same_domain_database(conn, direct_dsn)

        plan = plan_run(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            registry=self._registry,
        )
        if plan.is_noop:
            # A noop needs an id only for log correlation, and the engine will
            # never see this run — so this is the one id on this branch that is
            # ours rather than Hatchet's.
            from uuid import uuid4

            return noop_handle(str(uuid4()))

        dirty = [u.indicator for u in plan.units]
        features = {u.indicator: sorted(u.features) for u in plan.units}
        # ⚠ Still hashed from `features`, not from `plan_id`. The fingerprint must
        # name the *work*, and `plan_id` is minted fresh per submit — folding it
        # in would make every key unique and silently disable the idempotency
        # window. `tests/unit/test_hatchet_dedup_key.py` fails if that happens.
        dedup_key = _dedup_key(scenario_id, aoi_id, code_version, dirty, features)

        # ⚠ Before the plan is written, not after. A run refused here should
        # leave nothing behind, and this is a cheap engine read against a
        # database write plus a discard.
        workflow = hatchet_app.workflow_name(self._registry)
        if not _graph_is_deployed(workflow):
            raise GraphVersionUnavailable(
                f"no worker has ever registered {workflow!r} — this deployment "
                f"plans against a graph shape nothing has deployed"
            )

        # Committed before the workflow is triggered; see `_persist_plan`.
        plan_id = _persist_plan(direct_dsn, plan.units)
        payload = hatchet_app.RunInput(
            run_id="",  # filled from ctx.workflow_run_id; see the module docstring
            dirty=dirty,
            plan_id=plan_id,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            graph_version=hatchet_app.graph_version(self._registry),
            # What this key must name is **the set of rows two runs would fight
            # over**, because that is the whole hazard: the write path is
            # stale-then-insert, so overlapping writers leave features with the
            # wrong current value.
            #
            # A scenario, or failing that an AOI, is that set. With neither,
            # there is no shared scope to protect — two baseline runs conflict
            # only if they touch the same features, which is exactly what
            # `dedup_key` hashes.
            #
            # ⚠ The fallback is `dedup_key`, NOT a constant. `"baseline"` put
            # every scenario-less, AOI-less run in ONE bucket under
            # `max_runs=1` — so unrelated work serialized behind whichever
            # unscoped run got there first, and under the earlier
            # `CANCEL_NEWEST` strategy they cancelled each other outright.
            #
            # `dedup_key` is a hash over the dirty set and feature scope, so two
            # unscoped runs share a bucket exactly when they touch the same
            # features — which is the real conflict — and get their own
            # otherwise. That is the same set the fingerprint already names, so
            # no new concept is introduced to express it.
            concurrency_key=scenario_id or aoi_id or dedup_key,
            dedup_key=dedup_key,
        )
        try:
            ref = hatchet_app.get_run_workflow().run(
                payload,
                wait_for_result=False,
                # This is what buys back the run-level querying we lost by
                # dropping `orchestration_run`. It round-trips, it is readable
                # inside the workflow via `ctx.additional_metadata`, and it is a
                # filter in both `runs.list()` and the dashboard's search box
                # (verified, B8).
                #
                # Not a substitute for the table — you still cannot GROUP BY —
                # but "show me every run for this scenario" stops needing one.
                additional_metadata={
                    k: v
                    for k, v in {
                        "scenario_id": scenario_id,
                        "aoi_id": aoi_id,
                        "code_version": code_version,
                        # Filterable in `runs.list()` and the dashboard's search
                        # box, so "which runs used the graph before the retune"
                        # is a query rather than archaeology.
                        "graph_version": hatchet_app.graph_version(self._registry),
                    }.items()
                    if v is not None
                },
            )
        except IdempotencyCollisionError as collision:
            # A dedup hit is a *success*, not an error: the identical work is
            # already running and this caller wants to watch it. Returning the
            # existing run id is what makes the window useful — raising would
            # force every caller to handle an exception for the case the
            # feature exists to serve.
            #
            # The plan just written is provably unreferenced: the accepted run
            # carries the *earlier* submit's `plan_id`, and nothing will ever
            # look this one up. Discard it rather than leave an orphan for the
            # full retention horizon.
            _discard_plan(direct_dsn, plan_id)
            logger.info(
                "submit deduplicated onto run %s",
                collision.existing_run_external_id,
            )
            return RunHandle(run_id=collision.existing_run_external_id, status=None)
        except Exception as exc:  # noqa: BLE001 — re-raised below
            # ⚠ Retain on anything ambiguous. A deadline or an unavailable
            # engine may still have accepted the run, and a retry deduplicates
            # onto it — but only if the plan is still there to read. Deleting
            # here is unrecoverable; keeping a dead row for one horizon is not.
            if _is_definitely_not_accepted(exc):
                _discard_plan(direct_dsn, plan_id)
            raise
        return RunHandle(run_id=ref.workflow_run_id, status=None)

    # ── Read ────────────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> RunStatus | None:
        """Read the engine's own record; roll it up through the shared function.

        One construction of the unit list, whether the run is in flight or
        terminal. It used to be built twice — once from the finalizer's verdict
        and once from the engine's tasks — which is how the two came to disagree
        about what a unit was.

        `conn` is unused, and that is the point: this branch has no run tables.
        The signature keeps it because the port must stay implementable by a
        track that does.
        """
        from reimagina.orchestration import hatchet_app

        try:
            details = hatchet_app.get_hatchet().runs.get(run_id)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise HatchetUnavailable(f"could not read Hatchet run {run_id}") from exc
        if details is None:
            return None

        run = getattr(details, "run", details)
        engine_status = _name(getattr(run, "status", None))
        units = _units_of(details, skipped=_skipped_of(run))

        # ⚠ Two branches, deliberately. `overall_status` is a *terminal* roll-up:
        # it has no case for `running`, so an in-flight run falls through every
        # branch and comes back `failed`. While the engine still has the run
        # open, its own status is the answer; once closed, the units are.
        status = (
            overall_status(u.status for u in units)
            if engine_status in _ENGINE_TERMINAL
            else _RUN_STATUS_FOR.get(engine_status, "running")
        )
        return RunStatus(run_id=run_id, status=status, units=units)

    # ── Cancel ──────────────────────────────────────────────────────────────

    def cancel_run(self, conn: object, run_id: str) -> bool:
        """Ask the engine to stop, then stop the statement it cannot.

        Spike B4: cancelling marks the task CANCELLED immediately while the SQL
        keeps running, and the worker itself warns that its thread pool may
        block. So the engine's cancel is necessary and not sufficient —
        `pg_cancel_backend` against `job_run.backend_pid` (r031) is what
        actually ends the work.

        That table survives on every branch, which is the point worth carrying
        into the comparison: neither ops-plane solves this, and the answer lives
        outside both.
        """
        from reimagina.orchestration import hatchet_app

        current = self.get_run(run_id)
        if current is None or current.status in RUN_TERMINAL_STATES:
            return False

        hatchet_app.get_hatchet().runs.cancel(run_id)
        backend_for_connection(conn).job_run_repo().cancel_run_backends(run_id)
        return True

    def cancel_scenario(
        self, conn: object, scenario_id: str, *, lookback: timedelta = timedelta(days=7)
    ) -> list[str]:
        """Cancel every in-flight run for a scenario, by meaning rather than id.

        Not part of the port — Track A has no equivalent to offer, so putting it
        in the Protocol would force a hand-rolled implementation there purely to
        satisfy the shape. It is here because it is the operation an operator
        actually wants ("stop everything for this scenario"), and it is the
        concrete evidence for a comparison row: selecting work by *metadata*
        rather than by id is an engine capability, not something we wrote.

        **Snapshot semantics**: the candidate set is fixed when the operation
        starts — runs submitted after that are none of its business. It returns
        the subset it actually cancelled, so a run that reached a terminal state
        between the snapshot and its turn is absent rather than misreported as
        stopped.

        ⚠ This used to list ids, then call `bulk_cancel(filters=…)` with the
        same filter, then kill backends for the listed ids. Those are two
        different sets. `bulk_cancel` re-evaluates the filter *server-side* at
        an unknown later instant, so a run submitted in between was cancelled by
        the engine while its statement was never killed — a cancelled run whose
        PostGIS query keeps burning a connection, which is precisely the failure
        the second half exists to prevent. The set an operator was told about
        and the set that was acted on have to be the same set.

        Still two halves per run, because nothing interrupts a blocking psycopg2
        call: the engine cancel stops scheduling, `pg_cancel_backend` stops the
        SQL. That is `cancel_run`, so this reuses it rather than reimplementing
        the pair — one cancellation path, exercised by every caller.

        `lookback` bounds the listing; the engine's own retention default is
        720 h, so a week is generous for anything still running.
        """
        from hatchet_sdk import V1TaskStatus

        from reimagina.orchestration import hatchet_app

        listed = hatchet_app.get_hatchet().runs.list_with_pagination(
            since=datetime.now(timezone.utc) - lookback,
            statuses=[V1TaskStatus.RUNNING, V1TaskStatus.QUEUED],
            additional_metadata={"scenario_id": scenario_id},
        )
        snapshot = [
            rid
            for rid in (
                getattr(getattr(row, "metadata", None), "id", None) for row in listed
            )
            if rid
        ]

        return [run_id for run_id in snapshot if self.cancel_run(conn, run_id)]

    # ── Subscribe ───────────────────────────────────────────────────────────

    async def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Follow the run, ending on a terminal status.

        Push-driven, with one deliberate compromise. Hatchet's gRPC run-event
        listener delivers a `StepRunEvent` the moment a task starts, completes,
        fails or is cancelled — no polling interval, no wasted round-trips. But
        `StepRunEvent` carries only `(type, payload)`:
        `run_event_listener.py:66-68`. There is no task identity on it, so it
        cannot answer "which indicator", which is exactly what a `RunEvent` is.

        So the listener is the *wakeup* and `get_run` is the *answer*. That is
        strictly better than the fixed 1 s poll it replaces — an idle run costs
        nothing and a status change is reported immediately — while being honest
        that the engine's event feed alone is not sufficient.

        ⚠ The library track needs neither half: `DBOS.write_stream` is written
        by the workflow body, so it carries identity, ordering and a resumable
        offset. Reconstructing that here would mean every task publishing its
        own identity, i.e. rebuilding the vocabulary this track delegates.
        """
        from reimagina.orchestration import hatchet_app

        seen: dict[str, str] = {}

        async def emit(*, snapshot: bool = False) -> AsyncIterator[RunEvent]:
            """Diff the engine's current answer against what we already sent.

            ``snapshot`` marks the first read, taken before anything has been
            sent. If the run is *already* terminal then, the subscriber is late:
            it gets the outcome and nothing else. Replaying every unit
            transition first would be a history lesson for a caller that only
            asked how the run ended, and it would put a unit event where the
            port's contract puts the run-level one.
            """
            status = await asyncio.to_thread(self.get_run, run_id)
            if status is None:
                yield RunEvent(run_id=run_id, indicator=None, status="unknown")
                return
            terminal = status.status in RUN_TERMINAL_STATES
            if not (snapshot and terminal):
                for unit in status.units:
                    if seen.get(unit.indicator) != unit.status:
                        seen[unit.indicator] = unit.status
                        yield RunEvent(run_id, unit.indicator, unit.status, unit.detail)
            if terminal:
                yield RunEvent(run_id, None, status.status)

        # Whatever already happened before we attached.
        async for event in emit(snapshot=True):
            yield event
            if event.indicator is None:
                return

        listener = hatchet_app.get_hatchet().listener.stream(run_id)
        terminal_emitted = False
        reconcile_after_listener = True
        try:
            async for _wakeup in listener:
                async for event in emit():
                    yield event
                    if event.indicator is None:
                        terminal_emitted = True
                        listener.stop_signal = True
                        return
        except (asyncio.CancelledError, GeneratorExit):
            # An SSE client went away. Do not turn cleanup into an extra engine
            # request; the next subscriber takes its own authoritative snapshot.
            reconcile_after_listener = False
            raise
        finally:
            # A listener can hang up before the REST projection catches up. One
            # final reconciliation preserves the port's terminal-event contract.
            if reconcile_after_listener and not terminal_emitted:
                async for event in emit():
                    yield event


def _dedup_key(
    scenario_id: str | None,
    aoi_id: str | None,
    code_version: str,
    dirty: list[str],
    features: dict[str, list[str]],
) -> str:
    """Fingerprint the work, so only *identical* work deduplicates.

    Includes the dirty set and the per-unit feature scope, not just the
    scenario: two different edits to one scenario inside the TTL are different
    work and must both run. `sort_keys` because the same edit must fingerprint
    identically across processes; the feature lists are already sorted by the
    caller.
    """
    payload = json.dumps(
        {
            "scenario_id": scenario_id,
            "aoi_id": aoi_id,
            "code_version": code_version,
            "dirty": sorted(dirty),
            "features": features,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ⚠ No `_SCAFFOLDING` set and no `_strip_suffix`. Both existed to undo engine
# name mangling on `V1TaskSummary.display_name` — a millisecond suffix
# (`compactness-1785250189293`) and a finalizer rename (`summarise_failure` is
# reported as `summarise_failure-on-failure`). `V1WorkflowRunDetails.shape`
# carries `task_name`, the name as declared, and joins to `tasks` on
# `task_external_id`. Parsing a display string to recover a value the same
# response states plainly is a guess that breaks whenever the engine changes how
# it renders a name.
_SCAFFOLDING = frozenset({"plan", ""})


# Engine statuses from which no further task transition happens. Only then is
# `overall_status` meaningful — it is a terminal roll-up with no `running` case.
_ENGINE_TERMINAL = frozenset({"COMPLETED", "CANCELLED", "FAILED"})


def _skipped_of(run: object) -> frozenset[str]:
    """The finalizer's skip list, if it has run.

    The one fact `runs.get` cannot supply: a unit skipped for being non-dirty
    reports COMPLETED, identical to one that computed. Absent while the run is
    in flight, which is correct — nothing is known to be skipped until the
    engine has finished deciding what to schedule.
    """
    output = getattr(run, "output", None)
    if isinstance(output, dict):
        skipped = output.get("skipped")
        if isinstance(skipped, list):
            return frozenset(str(s) for s in skipped)
    return frozenset()


def _units_of(
    details: object, *, skipped: frozenset[str]
) -> tuple[UnitStatusView, ...]:
    """Per-unit outcomes, read from the engine rather than reconstructed.

    ``shape`` names the tasks as declared and ``tasks`` carries their outcomes;
    they join on ``task_external_id``. Going through ``shape`` is what removed
    the display-name parsing — and it also means a unit the engine has not
    created a task row for yet still appears, as ``queued``, instead of blinking
    into existence partway through the run.
    """
    outcomes = {
        getattr(task, "task_external_id", None): task
        for task in (getattr(details, "tasks", None) or [])
    }

    units: list[UnitStatusView] = []
    for item in getattr(details, "shape", None) or []:
        slug = getattr(item, "task_name", "") or ""
        if slug in _SCAFFOLDING:
            continue
        task = outcomes.get(getattr(item, "task_external_id", None))
        status = _UNIT_STATUS_FOR.get(_name(getattr(task, "status", None)), "queued")
        # A skip is invisible in the status enum: the engine reports COMPLETED.
        if slug in skipped:
            status = "skipped"
        units.append(UnitStatusView(slug, status, getattr(task, "error_message", None)))
    return tuple(sorted(units, key=lambda u: u.indicator))


def _name(value: object) -> str:
    """Enum member, bare string, or None -> a plain uppercase name."""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _is_not_found(exc: Exception) -> bool:
    """Keep unknown-run semantics separate from engine reachability failures."""
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return status == 404
