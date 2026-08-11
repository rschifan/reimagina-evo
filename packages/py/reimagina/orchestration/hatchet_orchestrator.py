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

from hatchet_sdk.clients.rest.models.v1_task_event_type import V1TaskEventType
from hatchet_sdk.clients.rest.models.v1_task_status import V1TaskStatus
from hatchet_sdk.clients.rest.models.v1_workflow_run_details import V1WorkflowRunDetails
from hatchet_sdk.exceptions import IdempotencyCollisionError

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.plan import noop_handle, plan_run
from reimagina.orchestration.database_identity import assert_same_domain_database
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    UNIT_TERMINAL_STATES,
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
        "FAILED_PRECONDITION",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "NOT_FOUND",
    }
)
# ⚠ `RESOURCE_EXHAUSTED` was here and had to go. gRPC raises it client-side for
# "Received message larger than max" — the RPC *succeeded*, the run exists, and
# only the reply could not be read. Treating that as proof of refusal deleted a
# live run's plan and killed every remaining unit with a missing-plan error,
# which is exactly the unrecoverable wrong-True this function must never make.
# It is genuinely ambiguous, so it retains.


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


def _graph_is_deployed(workflow: object) -> bool:
    """Has the workflow this submit will trigger ever been registered?

    Takes the **Workflow object**, not a name, so the thing checked and the
    thing submitted to cannot drift apart.

    ⚠ Registration, not current worker liveness. Gating submissions on a live
    ACTIVE worker looked stricter and was wrong: worker status moves moment to
    moment, so a healthy deployment with a busy or briefly-restarting worker
    refused runs it should have accepted — caught by a conformance run failing
    mid-suite with a worker running. A rejected submission destroys that
    request's work; a momentarily absent worker only delays it, because the
    engine queues. Liveness belongs in readiness, where failing merely takes the
    instance out of rotation.

    ⚠ Queried **by name**, not by scanning a page. `workflows.list()` sends no
    limit, the engine defaults it to 50 and orders arbitrarily, and this branch
    mints a new permanent workflow per graph shape plus one per bench workload —
    so the current name could fall off the page and refuse every submission on a
    correct deployment.

    ⚠ The **registered** name goes to the filter unchanged. An earlier version
    tried to recover a "bare" name by splitting on the first underscore, on the
    theory that the SDK would namespace the filter a second time. It does not:
    `ClientConfig.apply_namespace` returns the name untouched when it already
    starts with the namespace, so passing the registered form is exactly right.
    What the split did instead was destroy every name containing an underscore —
    `reimagina-bench-cpu_vs_io` became a query for `vs_io` — which refused every
    submit for four of the six bench workflows. That is the same "parse a
    rendered string to recover a value the response states plainly" mistake this
    module deleted `_strip_suffix` for.

    Fails **open** on an unreadable listing: an admission check must not become
    a submission outage.
    """
    from reimagina.orchestration import hatchet_app

    registered = getattr(workflow, "name", None)
    if not registered:
        return True

    try:
        listed = hatchet_app.get_hatchet().workflows.list(workflow_name=registered)
    except Exception:  # noqa: BLE001
        logger.warning(
            "could not list workflows; admitting run unchecked", exc_info=True
        )
        return True

    names = {getattr(w, "name", None) for w in (getattr(listed, "rows", None) or [])}
    return registered in names


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


# ⚠ Keyed on `V1TaskStatus` members, not on uppercased strings. The SDK types
# every status field as this enum, so a string key could only ever be reached by
# something that was not an engine response — which is precisely the case these
# maps must not silently absorb. A member missing from either map is a
# `KeyError` naming it, rather than a default that quietly reads as `queued`.
#
# Used while the run is still open. Once it is terminal the unit outcomes are
# rolled up through `port.overall_status` instead, which is the only way to
# reach `partial` — the enum has no such value and never will.
_RUN_STATUS_FOR = {
    V1TaskStatus.QUEUED: "queued",
    V1TaskStatus.RUNNING: "running",
    V1TaskStatus.COMPLETED: "succeeded",
    V1TaskStatus.CANCELLED: "cancelled",
    V1TaskStatus.FAILED: "failed",
}

# The whole adapter between the engine's task enum and the port's unit
# vocabulary. Five entries, one direction, no inference.
_UNIT_STATUS_FOR = {
    V1TaskStatus.QUEUED: "queued",
    V1TaskStatus.RUNNING: "running",
    # ⚠ COMPLETED covers both "computed" and "skipped because not dirty" — the
    # engine draws no distinction. The skip overlay from the event log is
    # applied on top of this in `_units_of`.
    V1TaskStatus.COMPLETED: "succeeded",
    V1TaskStatus.CANCELLED: "cancelled",
    V1TaskStatus.FAILED: "failed",
}

# Every member is mapped, so the maps cannot silently drift if the SDK adds one.
assert set(_RUN_STATUS_FOR) == set(V1TaskStatus) == set(_UNIT_STATUS_FOR)


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
        # ⚠ One object, used for both the check and the trigger. Deriving the
        # name independently — `workflow_name(self._registry)` here, but
        # `get_run_workflow()` at the trigger — meant validating a name nothing
        # was going to submit to. The workload suite proves it: it patches
        # `hatchet_app._dag` to a `reimagina-bench-*` workflow while passing its
        # own registry, so the check asked the engine about a `reimagina-run-v*`
        # name nobody had registered and refused every bench submit.
        declared = hatchet_app.get_run_workflow()
        if not _graph_is_deployed(declared):
            raise GraphVersionUnavailable(
                f"no worker has ever registered {getattr(declared, 'name', '?')!r} "
                f"— this deployment plans against a graph nothing has deployed"
            )

        # Committed before the workflow is triggered; see `_persist_plan`.
        plan_id = _persist_plan(direct_dsn, plan.units)
        # One derivation, two destinations: the run input and the metadata. It
        # is a json-dump plus a sha256, and computing it twice invited the two
        # copies to disagree after an edit to one of them.
        graph = hatchet_app.graph_version(self._registry)
        payload = hatchet_app.RunInput(
            run_id="",  # filled from ctx.workflow_run_id; see the module docstring
            dirty=dirty,
            plan_id=plan_id,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            graph_version=graph,
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
            # ⚠ A constant, and that is deliberate — the fingerprint was
            # tried here and is wrong. `dedup_key` hashes the *whole* dirty set
            # and feature map, so two unscoped runs share a bucket only when
            # their work is identical; overlapping-but-different feature sets
            # got different keys, ran concurrently, and raced on the features
            # they shared through the stale-then-insert write path.
            #
            # The original objection to the constant was that it made unrelated
            # baseline runs cancel each other — true under `CANCEL_NEWEST`, and
            # no longer true: `GROUP_ROUND_ROBIN` queues them instead. Queueing
            # unrelated work is a cost; corrupting a feature's current value is
            # not a cost, it is a defect.
            concurrency_key=scenario_id or aoi_id or "baseline",
            dedup_key=dedup_key,
        )
        try:
            ref = declared.run(
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
                        "graph_version": graph,
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

        engine_status = details.run.status
        units, scaffolding_failed = _units_of(details)

        # ⚠ `skipped` is excluded from the roll-up, because the word means two
        # different things across the tracks. On `sync` a skip is an
        # *unsatisfied input* — a real failure to compute. Here the engine
        # reports an unsatisfied dependent as CANCELLED, so a skip can only mean
        # "not dirty": an indicator the run was never asked to recompute.
        # Feeding those in made every ordinary run report `partial`.
        #
        # ⚠ Non-terminal statuses are excluded too. `overall_status` is a
        # terminal roll-up with no case for `queued` or `running`, so leaving
        # them in reported a run the engine had closed with an undispatched unit
        # as `failed`.
        settled = tuple(u.status for u in units if u.status in UNIT_TERMINAL_STATES)
        judged = tuple(st for st in settled if st != "skipped")

        # The engine's own answer is the default, and the units only better it
        # once every one of these holds:
        #
        #   still open              — the roll-up is terminal-only.
        #   scaffolding failed      — `plan` failing cancels every indicator, so
        #     the units show only cancellations and the run would read
        #     `cancelled` for something nobody cancelled.
        #   nothing settled         — every unit queued or running while the
        #     engine calls the run closed: a cancel before the tasks existed.
        #   everything settled, all skipped — work was asked for and not done,
        #     which is `failed`, as on `sync`.
        status = _RUN_STATUS_FOR[engine_status]
        if engine_status in _ENGINE_TERMINAL and not scaffolding_failed:
            if judged:
                status = overall_status(judged)
            elif units and len(settled) == len(units):
                status = "failed"
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

        # ⚠ Per-run failures are collected, not propagated. The comprehension
        # this replaces let one engine hiccup on run 7 of 20 abort the loop:
        # runs 1-6 were already cancelled *and their backends killed*, runs 8-20
        # were untouched, and the caller got an exception instead of the list —
        # so the operator had no record of which half had been stopped.
        cancelled: list[str] = []
        failures: list[str] = []
        for run_id in snapshot:
            try:
                if self.cancel_run(conn, run_id):
                    cancelled.append(run_id)
            except Exception:  # noqa: BLE001 — one run must not end the sweep
                failures.append(run_id)
                logger.warning("could not cancel run %s", run_id, exc_info=True)
        if failures:
            logger.warning(
                "cancel_scenario stopped %d run(s); %d could not be reached: %s",
                len(cancelled),
                len(failures),
                failures,
            )
        return cancelled

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

        # ponytail: re-reads the whole run on every wakeup; incremental only if
        # the catalog grows ~100x. `runs.get` has no flag to exclude payloads,
        # so each wakeup re-downloads every task event accumulated so far —
        # quadratic in task count over a run. Measured against this engine at
        # the shipped catalog (4 indicators, 5 tasks): 6-21 events, 15-29 KB
        # per call, events 11-32% of it, ~15 wakeups per run. That is under
        # 400 KB per subscriber per run, so the fix is not worth its
        # complexity yet. The upgrade path, when a catalog makes it worth it:
        # `v1_workflow_run_task_events_list` takes offset/limit, so the skip
        # set can be accumulated incrementally instead of re-derived — skips
        # are immutable once emitted. Revisit at ~100 indicators, or if a
        # subscriber's engine traffic shows up in a profile.
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


# Scaffolding is identified **structurally**, by how the SDK names it, not by a
# list of slugs and not by an allowlist of catalog indicators.
#
# A name blocklist was tried and is insufficient: the engine's `GetWorkflowShape`
# returns every Job of the workflow version, including the linked on-failure
# job, so both finalizers appear in `shape` under the names the SDK registered
# them with. An allowlist of catalog slugs was tried next and is worse — it
# *drops* anything it does not recognise, so an API replica one release behind
# its workers silently deleted the failing unit from a run and reported it
# `succeeded`, and a failed `plan` task vanished so the run read `cancelled`.
#
# The suffix rule below comes from the SDK's own registration
# (`on_success_task` registers `<name>-on-success`), so a graph that gains an
# indicator is reported rather than hidden, and scaffolding is still excluded.
_SCAFFOLDING_NAMES = frozenset({"plan", ""})
_SCAFFOLDING_SUFFIXES = ("-on-success", "-on-failure")


def _is_scaffolding(slug: str) -> bool:
    """Ours or the engine's, but never an indicator the caller asked for."""
    return slug in _SCAFFOLDING_NAMES or slug.endswith(_SCAFFOLDING_SUFFIXES)


# Engine statuses from which no further task transition happens. Only then is
# `overall_status` meaningful — it is a terminal roll-up with no `running` case.
_ENGINE_TERMINAL = frozenset(
    {V1TaskStatus.COMPLETED, V1TaskStatus.CANCELLED, V1TaskStatus.FAILED}
)


def _skipped_task_ids(details: V1WorkflowRunDetails) -> frozenset[str]:
    """External ids of tasks the engine skipped, from its own event log.

    ⚠ This replaced a finalizer, and the finalizer existed because of a
    misreading. `V1TaskEvent.task_id` is typed `StrictStr` while
    `V1TaskSummary.task_id` is `StrictInt`, which was taken to mean the two
    could not be joined — so a `on_success_task`/`on_failure_task` pair was
    written to publish `ctx.was_skipped` instead.

    The field is simply misnamed: `V1TaskEvent.task_id` carries the
    **`task_external_id`**, not the integer `task_id`. Measured against this
    engine: 489 task events across 19 runs, every one of them joining to a task
    by external id, and none by the integer.

    Two properties follow, both better than the finalizer had:

    * a skip is visible **while the run is in flight**, not only once a
      finalizer has run at terminal state;
    * `SKIPPED` marks exactly the `skip_if` skips. Verified: of the tasks
      carrying the event, 62 of 62 were `COMPLETED`, and no `CANCELLED` or
      `FAILED` task carried one — so a dependent the engine cancelled is not
      relabelled as a skip.
    """
    return frozenset(
        event.task_id
        for event in details.task_events
        if event.event_type is V1TaskEventType.SKIPPED
    )


def _units_of(details: V1WorkflowRunDetails) -> tuple[tuple[UnitStatusView, ...], bool]:
    """Per-unit outcomes, plus whether any scaffolding task failed.

    ``shape`` names the tasks as declared and ``tasks`` carries their outcomes;
    they join on ``task_external_id``. Going through ``shape`` means a unit the
    engine has not created a task row for yet still appears, as ``queued``,
    instead of blinking into existence partway through the run.

    ⚠ Returns the scaffolding-failed flag rather than swallowing it. `plan` is
    filtered out of the unit list, so when *it* fails the engine cancels every
    indicator and the roll-up sees only cancellations — reporting `cancelled`
    for a run nobody cancelled. The caller defers to the engine instead.
    """
    # ⚠ Keyed directly. `task_external_id` is a required `str` on both
    # `V1TaskSummary` and the shape item, so there is no id-less row to defend
    # against — and the defence was the defect: an earlier version keyed on
    # `getattr(..., None)`, which filed every row it failed to read under `None`
    # and then joined an id-less shape item to whichever of them came last. The
    # guard existed to repair damage only the guard could cause.
    outcomes = {task.task_external_id: task for task in details.tasks}

    skipped = _skipped_task_ids(details)

    units: list[UnitStatusView] = []
    scaffolding_failed = False
    for item in details.shape:
        slug = item.task_name
        external_id = item.task_external_id
        task = outcomes.get(external_id)
        # No task row yet: the engine has not created one for a unit it has not
        # reached. `queued` is the honest reading, and it is the only case that
        # needs a default — a task that exists always carries a mapped status.
        status = _UNIT_STATUS_FOR[task.status] if task is not None else "queued"
        # A skip reports COMPLETED like anything else that finished, so the
        # event log is the only thing that separates "not dirty" from
        # "computed". Applied here rather than overlaid by the caller, so the
        # unit list is right the first time it is built.
        if external_id in skipped:
            status = "skipped"
        if _is_scaffolding(slug):
            scaffolding_failed = scaffolding_failed or status == "failed"
            continue
        units.append(UnitStatusView(slug, status, task.error_message if task else None))
    return tuple(sorted(units, key=lambda u: u.indicator)), scaffolding_failed


def _is_not_found(exc: Exception) -> bool:
    """Keep unknown-run semantics separate from engine reachability failures."""
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return status == 404
