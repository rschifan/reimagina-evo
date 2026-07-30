"""The Orchestrator port over Hatchet.

The engine owns run state, so ``get_run`` reads no table of ours. What it reads
is **the run's output, not its status** — and that distinction is the whole
correction to the first cut of this module.

The first version mapped Hatchet's status enum onto our vocabulary and
documented, at length, what the enum could not say: no ``partial``, no
``skipped``. Both statements are true of the enum and both were the wrong thing
to build on. ``on_failure_task`` runs precisely when some units succeeded and
one did not, ``ctx.was_skipped`` separates "not dirty" from "computed", and the
finalizer's return value is what ``runs.get_result()`` serves — even for a run
whose status is FAILED. Measured in spike B8.

So the vocabulary survives. What actually differs from the library track is
**where it lives**:

    Track A  our table   — joinable, indexable, aggregable with SQL
    Track B  run output  — one run at a time, through the engine

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


class HatchetUnavailable(RuntimeError):
    """Hatchet could not answer a request for run state."""


# Used only until the finalizer has written a verdict — i.e. while the run is
# still going. Once it is terminal the verdict supersedes all of this.
_RUN_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETED": "succeeded",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}

_UNIT_STATUS_FOR = {
    "QUEUED": "queued",
    "RUNNING": "running",
    # ⚠ In-flight only. COMPLETED covers both "computed" and "skipped because
    # not dirty"; the finalizer's `ctx.was_skipped` is what tells them apart,
    # and it has not run yet at this point.
    "COMPLETED": "succeeded",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}


class HatchetOrchestrator:
    """Scheduling, run state and the verdict all belong to the engine."""

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
        # Committed before the workflow is triggered; see `_persist_plan`.
        plan_id = _persist_plan(direct_dsn, plan.units)
        payload = hatchet_app.RunInput(
            run_id="",  # filled from ctx.workflow_run_id; see the module docstring
            dirty=dirty,
            plan_id=plan_id,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
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
            # ⚠ The constant `"baseline"` used here first was wrong, and badly:
            # it put every scenario-less run in ONE bucket under
            # `max_runs=1, CANCEL_NEWEST`, so unrelated baseline runs cancelled
            # each other. In the conformance suite that showed up as a poisoned
            # unit reporting `cancelled` rather than `failed`; in production it
            # would have meant a second AOI's recompute silently killing the
            # first's.
            # An unscoped baseline has no proven partition boundary. Serialize
            # it conservatively rather than allowing overlapping feature sets to
            # race merely because their fingerprints differ.
            concurrency_key=scenario_id or aoi_id or "baseline",
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
            logger.info(
                "submit deduplicated onto run %s",
                collision.existing_run_external_id,
            )
            return RunHandle(run_id=collision.existing_run_external_id, status=None)
        return RunHandle(run_id=ref.workflow_run_id, status=None)

    # ── Read ────────────────────────────────────────────────────────────────

    def get_run(self, conn: object, run_id: str) -> RunStatus | None:
        """Prefer the finalizer's verdict; fall back to status while in flight.

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

        verdict = _verdict_of(run)
        if verdict is not None:
            return RunStatus(
                run_id=run_id,
                status=verdict["overall"],
                units=tuple(
                    sorted(
                        (
                            UnitStatusView(
                                slug, u.get("status", "queued"), u.get("detail")
                            )
                            for slug, u in verdict.get("units", {}).items()
                        ),
                        key=lambda u: u.indicator,
                    )
                ),
            )

        # Still running: no verdict yet, so translate what the engine has.
        units: list[UnitStatusView] = []
        for task in getattr(details, "tasks", None) or []:
            slug = getattr(task, "display_name", None) or getattr(task, "step_id", "")
            slug = slug.rsplit(":", 1)[-1]  # "workflow:task" -> "task"
            # ⚠ The engine suffixes a timestamp to display_name
            # ("compactness-1785250189293"), and the finalizer tasks are not
            # indicators. Both must go or callers see phantom units.
            slug = _strip_suffix(slug)
            if slug in _SCAFFOLDING:
                continue
            units.append(
                UnitStatusView(
                    slug,
                    _UNIT_STATUS_FOR.get(
                        _name(getattr(task, "status", None)), "queued"
                    ),
                    getattr(task, "error_message", None),
                )
            )

        return RunStatus(
            run_id=run_id,
            status=_RUN_STATUS_FOR.get(engine_status, "running"),
            units=tuple(sorted(units, key=lambda u: u.indicator)),
        )

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

        current = self.get_run(conn, run_id)
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

        Still two halves, because B4 has not stopped being true — the engine
        cancel is one call, the statement kill is per-run. `lookback` exists
        because `RunFilter.since` is required; the engine's own default
        retention is 720 h, so a week is generous for anything still running.
        """
        from hatchet_sdk import BulkCancelReplayOpts, RunFilter, V1TaskStatus

        from reimagina.orchestration import hatchet_app

        client = hatchet_app.get_hatchet().runs
        since = datetime.now(timezone.utc) - lookback
        live = [V1TaskStatus.RUNNING, V1TaskStatus.QUEUED]
        meta = {"scenario_id": scenario_id}

        listed = client.list_with_pagination(
            since=since, statuses=live, additional_metadata=meta
        )
        ids = [
            rid
            for rid in (
                getattr(getattr(row, "metadata", None), "id", None) for row in listed
            )
            if rid
        ]

        client.bulk_cancel(
            BulkCancelReplayOpts(
                filters=RunFilter(since=since, statuses=live, additional_metadata=meta)
            )
        )
        repo = backend_for_connection(conn).job_run_repo()
        for run_id in ids:
            repo.cancel_run_backends(run_id)
        return ids

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
            status = await asyncio.to_thread(self.get_run, None, run_id)
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


_SCAFFOLDING = frozenset(
    {
        "plan",
        "",
        # ⚠ The engine renames a finalizer: `on_failure_task` named
        # `summarise_failure` is reported as `summarise_failure-on-failure`.
        # Listing the bare names let `summarise_failure-on-failure` through as
        # an indicator, and a caller asking what happened to its run got a unit
        # it had never heard of.
        "summarise_success-on-success",
        "summarise_failure-on-failure",
    }
)


def _strip_suffix(name: str) -> str:
    """`compactness-1785250189293` -> `compactness`.

    The engine appends a millisecond timestamp to task display names. Only strip
    a trailing all-digit segment, so an indicator slug that legitimately ends in
    a hyphenated word survives.
    """
    head, _, tail = name.rpartition("-")
    return head if head and tail.isdigit() else name


def _verdict_of(run: object) -> dict | None:
    """The finalizer's output, if it has run and looks like a verdict."""
    output = getattr(run, "output", None)
    if isinstance(output, dict) and "overall" in output and "units" in output:
        return output
    return None


def _name(value: object) -> str:
    """Enum member, bare string, or None -> a plain uppercase name."""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _is_not_found(exc: Exception) -> bool:
    """Keep unknown-run semantics separate from engine reachability failures."""
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return status == 404
