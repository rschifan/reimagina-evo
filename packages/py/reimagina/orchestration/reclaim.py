"""Reclaim work abandoned by an executor that will not come back.

``DBOS.launch()`` recovers **this** executor's interrupted workflows, and only
this executor's. That covers a restart; it does not cover a death. A worker
whose host is destroyed, or whose replica is scaled away, leaves its PENDING
workflows recorded against an ``executor_id`` that no process will ever claim
again, and the run they belong to never reaches a terminal state. Nothing
times it out, because from the database's point of view the work is still in
progress.

This module closes that. A live executor periodically asks which executors
have stopped publishing liveness, and asks DBOS to recover their pending
workflows onto itself.

**Why it was not built until now.** ``docs/product/PHASE3_OPS_PLANE_DBOS_SPIKES.md``
measured the sweep working against DBOS 2.28 (spike 1: two handles returned,
the run driven to SUCCESS, exactly one unit recomputed) and then deliberately
shelved it. 2.28.0 shipped six hours before the idempotent-recovery fix
merged, so two processes sweeping the same dead executor could each start a
runner for the same workflow. The recommendation was to build it last, gated
on a conservative liveness signal.

DBOS 3 changes the first half of that. ``dbos._recovery._recover_workflow``
now re-enqueues rather than executing directly, "so that every recovered
workflow starts through the queue's atomic ENQUEUED->PENDING dequeue. That
handoff admits exactly one runner, which makes duplicate recovery requests
idempotent." Two sweepers racing on the same dead executor is therefore safe.

⚠ **It does not make sweeping a *live* executor safe, and that is the whole
reason the liveness gate survives.** Re-enqueuing a workflow that a running
process is still executing admits a second runner alongside the first. The
idempotency fix is about duplicate *recovery requests*, not about recovering
work that was never abandoned. So the gate is a correctness requirement, not
a politeness, and its threshold is deliberately many multiples of the beat
interval: a late heartbeat is cheap, a wrongly-declared death is not.

The gate reads ``jobs_runtime.ops_executor_heartbeat``, which every process
that launches an executor now writes — see ``dbos_app.launch``. An executor
with no row at all is never swept: absence is not evidence of death, and the
one process that could be in that state is one running code old enough not to
publish liveness, which is exactly when guessing is worst.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field

import psycopg2

from reimagina.orchestration.dbos_app import _executor_id, dbos_dsn

logger = logging.getLogger(__name__)

#: How stale a heartbeat must be before its executor is presumed dead.
#:
#: The default is 10x the default beat interval of 30 s. The asymmetry is
#: deliberate: waiting five minutes to reclaim a dead worker's run costs five
#: minutes; reclaiming a live worker's run costs a second concurrent execution
#: of the same unit. Raise it, never lower it, unless the beat interval moves
#: with it.
DEFAULT_STALE_AFTER_S = 300.0

#: Minimum ratio of staleness threshold to beat interval. Enforced rather than
#: documented, because the two are configured by separate environment
#: variables and nothing else would notice them drifting together.
MIN_STALE_RATIO = 4.0


@dataclass(frozen=True)
class ReclaimReport:
    """What one sweep found and did."""

    dead_executors: tuple[str, ...] = ()
    workflows_recovered: int = 0
    #: Dead executors whose liveness row was deliberately *kept*, because they
    #: still have unfinished workflows this process could not recover — almost
    #: always a version mismatch. See ``forget_executor``.
    stranded_executors: tuple[str, ...] = ()
    skipped_reason: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def acted(self) -> bool:
        return bool(self.dead_executors)


def stale_after_seconds() -> float:
    """The configured staleness threshold, validated against the beat interval."""
    configured = float(
        os.environ.get("REIMAGINA_EXECUTOR_STALE_S", DEFAULT_STALE_AFTER_S)
    )
    interval = float(os.environ.get("REIMAGINA_EXECUTOR_HEARTBEAT_S", "30"))
    if configured < interval * MIN_STALE_RATIO:
        raise RuntimeError(
            f"REIMAGINA_EXECUTOR_STALE_S={configured} is less than "
            f"{MIN_STALE_RATIO:g}x the heartbeat interval ({interval}s). A "
            "process that merely misses a beat would be declared dead and its "
            "running work re-enqueued alongside it."
        )
    return configured


def dead_executors(
    conn, *, stale_after: float, exclude: str | None = None
) -> list[str]:
    """Executors whose last heartbeat is older than ``stale_after`` seconds.

    ``exclude`` is this process, which is never a candidate however stale its
    own row looks — a sweeper that declared itself dead would re-enqueue the
    work it is running.

    Ordered oldest first so a sweep that is interrupted part-way has made
    progress on the longest-abandoned work.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT executor_id
            FROM jobs_runtime.ops_executor_heartbeat
            WHERE heartbeat_at < clock_timestamp()
                                 - make_interval(secs => %s)
              AND (%s::text IS NULL OR executor_id <> %s)
            ORDER BY heartbeat_at
            """,
            (stale_after, exclude, exclude),
        )
        return [row[0] for row in cur.fetchall()]


#: Workflow states that still belong to the executor that recorded them.
#: ``PENDING`` is in flight; ``ENQUEUED`` is waiting for a dequeue. Both are
#: work that no terminal state covers.
_UNFINISHED = ("PENDING", "ENQUEUED")


def unfinished_workflows(conn, executor_id: str) -> list[tuple[str, str]]:
    """``(workflow_uuid, application_version)`` still open against an executor.

    Read from ``dbos.workflow_status`` directly rather than through DBOS,
    because DBOS's own reader is the thing we are checking. See
    ``forget_executor``.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT workflow_uuid, COALESCE(application_version, '')
            FROM dbos.workflow_status
            WHERE executor_id = %s
              AND status IN ({", ".join(["%s"] * len(_UNFINISHED))})
            """,  # noqa: S608 — interpolation is a fixed literal tuple, not input
            (executor_id, *_UNFINISHED),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def forget_executor(conn, executor_id: str) -> bool:
    """Drop a reclaimed executor's liveness row, but only once it is empty.

    Without this the same dead executor is re-swept on every pass forever, and
    its row keeps showing in ``explore.ops_executor_health_v`` as a
    permanently dead worker — noise an operator has to learn to ignore.
    Removing it is otherwise safe: the row is liveness, not history; the run's
    own provenance lives in ``jobs_runtime.orchestration_run``.

    ⚠ **Recovering zero workflows is not evidence that there were none.**
    ``DBOS._recover_pending_workflows`` asks
    ``get_pending_workflows(executor_id, app_version)``, and that query filters
    on ``application_version = <this process's version>``. A sweeper running a
    different version than the executor that died — precisely the blue-green
    case the version pinning in ``dbos_app.config`` exists to support —
    recovers nothing, every time, no matter how much work is stranded.

    Deleting the row on that result destroyed the last pointer to it: the
    workflows stay PENDING forever, the sweep never looks at that id again,
    and ``ops_executor_health_v`` stops showing there was ever a worker there.
    A silent, permanent leak produced by the cleanup step.

    So the row is dropped only when the executor genuinely has nothing left
    open. Returns whether it was dropped, so the caller can say why not.
    """
    if unfinished_workflows(conn, executor_id):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM jobs_runtime.ops_executor_heartbeat WHERE executor_id = %s",
            (executor_id,),
        )
    return True


def sweep(*, dry_run: bool = False) -> ReclaimReport:
    """One pass: find dead executors, recover their pending workflows.

    Requires ``dbos_app.launch()`` to have run in this process — recovery
    re-enqueues onto the caller's own executor, so there has to be one.

    Returns a report rather than raising on a per-executor failure: one
    unreachable executor's workflows must not stop the others being reclaimed,
    and the caller is a loop that will come round again.
    """
    from dbos import DBOS

    threshold = stale_after_seconds()
    me = _executor_id()
    recovered = 0
    errors: list[str] = []
    stranded: list[str] = []

    with contextlib.closing(psycopg2.connect(dbos_dsn())) as conn:
        dead = dead_executors(conn, stale_after=threshold, exclude=me)
        if not dead:
            return ReclaimReport(skipped_reason="no executor is stale")
        if dry_run:
            return ReclaimReport(dead_executors=tuple(dead), skipped_reason="dry run")
        for executor in dead:
            try:
                handles = DBOS._recover_pending_workflows([executor])
            except Exception as exc:  # noqa: BLE001 — one bad id must not stop the rest
                errors.append(f"{executor}: {exc}")
                logger.warning(
                    "could not reclaim workflows from executor %r",
                    executor,
                    exc_info=True,
                )
                continue
            recovered += len(handles)
            logger.info(
                "reclaimed %d workflow(s) from executor %r (last beat older than %gs)",
                len(handles),
                executor,
                threshold,
            )
            left = unfinished_workflows(conn, executor)
            if left:
                # The row stays. `forget_executor` enforces this too — it is
                # the safe primitive — but the detail for the log has to be
                # read anyway, so read it once and decide here.
                versions = sorted({v for _, v in left if v})
                stranded.append(executor)
                logger.warning(
                    "executor %r still has %d unfinished workflow(s) after "
                    "recovery%s; keeping its liveness row so the next sweep "
                    "retries. Recovery only reclaims workflows whose "
                    "application_version matches this process (%s), so this is "
                    "what a cross-version strand looks like: drain it with a "
                    "process pinned to the stranded version.",
                    executor,
                    len(left),
                    f" (versions {', '.join(versions)})" if versions else "",
                    os.environ.get("REIMAGINA_APP_VERSION", "unpinned"),
                )
            else:
                forget_executor(conn, executor)
            conn.commit()

    return ReclaimReport(
        dead_executors=tuple(dead),
        workflows_recovered=recovered,
        stranded_executors=tuple(stranded),
        errors=tuple(errors),
    )
