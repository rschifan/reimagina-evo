"""DBOS ops-plane — durable execution as a library, with no standing service.

Checkpoints land in the same Postgres as the domain data, under the ``dbos``
schema, so there is nothing to deploy and nothing to back up separately. We keep
the wave scheduler (``orchestration.wave``) and borrow only durability: this
module is what an engine that owned scheduling would let us delete.

Both the API and the worker import it, because DBOS can only recover or start a
workflow whose definition is present in the process doing so.

**The replay rule that shapes everything here.** DBOS matches replayed steps by
``(position, function name)``. Our design calls the *same* step function once per
indicator, so position alone decides which checkpointed output a replayed call
receives. A workflow body that decided its steps from a mutable module global
would therefore not fail loudly on a catalog change mid-run — it would hand one
indicator's result to another, silently. Measured, not theorised; see
``docs/product/PHASE3_OPS_PLANE_DBOS_SPIKES.md`` spike 2.

So: **the workflow body branches only on workflow inputs and step results.**
Reading a global *inside a step* is fine, because a step's output is
checkpointed — it is the body's control flow that must be reproducible.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import psycopg2
from dbos import DBOS, DBOSConfig, Queue

from reimagina.jobs._lifecycle import current_run
from reimagina.orchestration import runstore
from reimagina.orchestration.port import RunEvent, overall_status
from reimagina.orchestration.runners import default_runners
from reimagina.orchestration.wave import plan_wave

PROGRESS_STREAM = "progress"
UNITS_QUEUE = "units"

# One dirty unit, projected to something JSON/pickle-safe. `deps` is carried so
# the workflow body can rebuild the dependency graph for `plan_wave` without
# reading the catalog — see the replay rule above.
UnitInput = dict[str, Any]


def dbos_dsn() -> str:
    """Direct Postgres DSN for checkpoints. Fails loudly rather than silently.

    ⚠ DBOS falls back to a local SQLite file when no database URL is set, so a
    missing environment variable in production would produce a working process
    with private, unrecoverable checkpoints instead of an error.

    LISTEN/NOTIFY backs the progress streams and cannot cross PgBouncer in
    transaction mode, so this must be the direct DSN.
    """
    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError(
            "the DBOS ops-plane requires a direct Postgres DSN — set "
            "DATABASE_URL_DIRECT (checkpoints and progress streams both need a "
            "real server connection, and DBOS silently uses SQLite without one)"
        )
    return dsn


def _executor_id() -> str:
    """Identify this process. DBOS defaults every process to ``local``.

    Recovery is scoped by executor id, so leaving the default makes every
    process claim to be the same one — which turns "recover what this process
    was running" into "recover what anything was running".
    """
    return (
        os.environ.get("REIMAGINA_EXECUTOR_ID")
        or f"{os.uname().nodename}-{os.getpid()}"
    )


def config() -> DBOSConfig:
    cfg: DBOSConfig = {
        "name": "reimagina",
        "system_database_url": dbos_dsn(),
        "dbos_system_schema": "dbos",
        "executor_id": _executor_id(),
        "run_admin_server": False,
    }
    # Pinning gates recovery: an unpinned version is an MD5 of the registered
    # workflow source, so any edit strands in-flight runs. Pinned, they become
    # recoverable onto possibly-incompatible code — which is why the deploy
    # story is blue-green, with old-version processes kept alive to drain.
    if os.environ.get("REIMAGINA_APP_VERSION"):
        cfg["application_version"] = os.environ["REIMAGINA_APP_VERSION"]
    return cfg


_queue: Queue | None = None


def launch() -> None:
    """Register config, start DBOS, and bind the unit queue. Idempotent."""
    global _queue
    if _queue is not None:
        return
    DBOS(config=config())
    DBOS.launch()
    # After launch, per the deprecation of constructing Queue() directly.
    # worker_concurrency, not concurrency: the global limit also counts PENDING
    # workflows from earlier application versions, which a blue-green deploy
    # deliberately leaves lying around.
    _queue = DBOS.register_queue(
        UNITS_QUEUE,
        worker_concurrency=int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "2")),
    )


def units_queue() -> Queue:
    if _queue is None:
        raise RuntimeError("dbos_app.launch() must run before a run is submitted")
    return _queue


def _pg():
    conn = psycopg2.connect(dbos_dsn())
    conn.autocommit = True
    return conn


class _Spec:
    """The slice of an IndicatorSpec the wave scheduler needs.

    Rebuilt from the workflow's own input rather than imported from the
    catalog, so a catalog edit mid-run cannot change how the run is scheduled.
    """

    __slots__ = ("slug", "depends_on")

    def __init__(self, slug: str, deps: Sequence[str]) -> None:
        self.slug = slug
        self.depends_on = tuple((dep, 0.0) for dep in deps)  # weights unused here


def _as_registry(units: Sequence[UnitInput]) -> tuple[_Spec, ...]:
    return tuple(_Spec(u["slug"], u["deps"]) for u in units)


def _emit(run_id: str, indicator: str | None, status: str, detail: str | None = None):
    """Publish progress in the port's own shape.

    Called from the workflow body, never a step: a stream write from a workflow
    is exactly-once, while from a step it is at-least-once, and a duplicated
    terminal event would close a subscriber's stream early.
    """
    DBOS.write_stream(
        PROGRESS_STREAM, asdict(RunEvent(run_id, indicator, status, detail))
    )


# ── Steps: every database touch lives in one ────────────────────────────────
#
# A workflow body that read Postgres directly would re-read on replay and could
# see different data than it decided with. DBOS enforces this — a bare DB call
# in a body raises DBOSUnexpectedStepError.


@DBOS.step()
def s_open_run(run_id: str, units: list[UnitInput], params: dict) -> None:
    """Persist the run envelope and its units. Track A keeps its own run state,
    which is what preserves the `skipped` / `partial` vocabulary."""
    with _pg() as conn:
        runstore.create_run(conn, run_id=run_id, status="running", **params)
        runstore.record_units(
            conn,
            run_id=run_id,
            units=[
                {"indicator": u["slug"], "features": u["features"], "status": "queued"}
                for u in units
            ],
        )


@DBOS.step()
def s_set_status(
    run_id: str, slug: str, status: str, detail: str | None = None
) -> bool:
    with _pg() as conn:
        return runstore.set_unit_status(
            conn, run_id=run_id, indicator_slug=slug, status=status, detail=detail
        )


@DBOS.step()
def s_finalize(run_id: str, status: str) -> bool:
    with _pg() as conn:
        return runstore.finalize_run(conn, run_id=run_id, status=status)


@DBOS.step(retries_allowed=False)
def s_compute(run_id: str, slug: str, features: list[str], params: dict) -> None:
    """Run one indicator's real compute. Minutes of blocking PostGIS.

    Not retried: a runner exception is deterministic, so a retry would burn the
    same minutes to reach the same failure. Infrastructure faults surface as a
    failed connect, which is retried by the step that owns it.

    ``current_run`` is what lets a cancel find this work: the runner opens its
    own job_run row, and the ambient run id plus ``pg_backend_pid()`` are how
    ``cancel_run`` locates the backend to signal (migration r031).
    """
    runner = default_runners().get(slug)
    if runner is None:
        raise LookupError(f"no runner registered for {slug!r}")
    with _pg() as conn, current_run(run_id):
        runner(
            conn,
            features=frozenset(features),
            scenario_id=params.get("scenario_id"),
            code_version=params.get("code_version", "dev"),
            aoi_id=params.get("aoi_id"),
        )


# ── Workflows ───────────────────────────────────────────────────────────────


@DBOS.workflow()
def unit_workflow(run_id: str, slug: str, features: list[str], params: dict) -> str:
    s_set_status(run_id, slug, "running")
    try:
        s_compute(run_id, slug, features, params)
    except Exception as exc:
        s_set_status(run_id, slug, "failed", _safe_detail(exc))
        raise
    s_set_status(run_id, slug, "succeeded")
    return "succeeded"


def _safe_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


@DBOS.workflow()
def run_workflow(run_id: str, units: list[UnitInput], params: dict) -> str:
    """Drive the dirty-set to completion, one wave at a time.

    ``units`` is a workflow *input*, so the loop below is reproducible on
    replay no matter what the catalog does meanwhile. Everything the body
    branches on is either that input or a checkpointed step result.
    """
    s_open_run(run_id, units, params)

    registry = _as_registry(units)
    order = [u["slug"] for u in units]
    features = {u["slug"]: u["features"] for u in units}
    statuses = {slug: "queued" for slug in order}

    while True:
        wave = plan_wave([(slug, statuses[slug]) for slug in order], registry)

        for slug in wave.skip:
            statuses[slug] = "skipped"
            s_set_status(run_id, slug, "skipped", "unsatisfied input")
            _emit(run_id, slug, "skipped", "unsatisfied input")

        if not wave.dispatch:
            break

        # Enqueue in a fixed order and await in the same one. Child workflow ids
        # are positional (parent_id + function_id), so an as-completed loop would
        # shift the ids of everything after it and mint duplicate children.
        handles = [
            (
                slug,
                units_queue().enqueue(
                    unit_workflow, run_id, slug, features[slug], params
                ),
            )
            for slug in wave.dispatch
        ]
        for slug, handle in handles:
            try:
                handle.get_result()
                statuses[slug] = "succeeded"
            except Exception:  # noqa: BLE001 — fault isolation is the point
                statuses[slug] = "failed"
            _emit(run_id, slug, statuses[slug])

    final = overall_status(statuses.values())
    s_finalize(run_id, final)
    _emit(run_id, None, final)
    DBOS.close_stream(PROGRESS_STREAM)
    return final
