"""DBOS ops-plane — durable execution as a library, with no standing service.

Checkpoints land in the same Postgres as the domain data, under the ``dbos``
schema, so there is nothing to deploy and nothing to back up separately. We keep
the wave scheduler (``orchestration.wave``) and borrow only durability: this
module is what an engine that owned scheduling would let us delete.

Executors import this module to recover and run workflow definitions. API
processes use ``DBOSClient`` to enqueue named workflows and do not launch an
executor in the production-default ``client`` mode.

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

import contextlib
import logging
import os
import threading
from contextlib import contextmanager
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from graphlib import CycleError
from typing import Any, Literal, NotRequired, TypedDict

import psycopg2
from dbos import DBOS, DBOSConfig, Queue, SetWorkflowID

from reimagina import settings
from reimagina.jobs._lifecycle import current_run, record_failed_job_run
from reimagina.orchestration import runstore
from reimagina.orchestration.graph import topological_order
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    RunEvent,
    overall_status,
)
from reimagina.orchestration.runners import default_runners
from reimagina.orchestration.wave import plan_wave
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)

PROGRESS_STREAM = "progress"
RUNS_QUEUE = "runs"
UNITS_QUEUE = "units"

# DBOS dispatch is poll-only — nothing pushes. Two independent 1.0 s defaults sit
# on the critical path of every wave: the queue thread's dequeue loop
# (`_queue.py` DEFAULT_QUEUE_POLLING_INTERVAL_SEC) and the parent's wait on a
# child (`_core.py` DEFAULT_POLLING_INTERVAL). Both must be lowered together —
# measured, lowering either alone leaves ~1.18 s per wave, because the wave pays
# whichever is still slow.
#
# Measured, 4 competing executors on one queue vs idle load on a 2-queue app:
#
#   interval   per-wave    idle txn/s    backoffs
#   1.0        1183 ms     15.8          0
#   0.25        312 ms     39.6          0
#   0.1         137 ms     82.8          0      <- chosen
#   0.05         73 ms    149.9          0
#   0.01         24 ms    567.3          0
#
# 0.1 s takes 8.6x of the available latency for 5x the idle load; 0.01 s costs
# 36x to buy another 113 ms nobody can perceive.
#
# Measured end to end on the real catalog (13 indicators, 7 groups, 1500 blocks,
# 3 reps each, same process, sequential so the arms cannot contend):
#
#                    1.0 s     0.1 s
#   makespan       6958 ms   3538 ms   1.97x
#   busy           4904 ms   4913 ms   1.00x  (same work, as it must be)
#   head-of-line   2008 ms    345 ms   5.8x
#   settle lag      956 ms    144 ms   6.7x
#   parallelism       0.94      1.58
#
# The parallelism row is the point, and it is not what the interval looks like
# it does. Polling did not merely add dead time between waves — it *serialized
# units that could have overlapped*, because a unit finishing mid-interval sat
# until the next tick before the next one was dequeued. That is why makespan
# fell by 3.4 s when only ~2.1 s of the run was idle waiting.
#
# ⚠ Zero contention above is *conditional on `worker_concurrency`*. Dequeue uses
# `with_for_update(skip_locked=queue._concurrency is None)`, so with only a
# worker limit set, competing executors skip locked rows instead of failing.
# Setting a global `concurrency` on these queues flips that to `nowait=True`,
# where a fast poll turns into real serialization failures. Don't.
POLL_INTERVAL_SEC = float(os.environ.get("REIMAGINA_POLL_INTERVAL_SEC", "0.1"))

ExecutionMode = Literal["client", "executor", "dev"]


class UnitInput(TypedDict):
    """Stable, JSON-safe workflow input for one dirty indicator."""

    slug: str
    features: list[str]
    deps: list[str]


class RunParams(TypedDict):
    """Stable workflow input. Add fields here before a workflow consumes them."""

    plan_id: NotRequired[str]
    """Handle for the resolved feature set in ``jobs_runtime.run_plan``.

    ⚠ ``NotRequired`` because a run submitted before this field existed can be
    resumed after it: those inputs still carry their ids in ``UnitInput.features``
    and ``s_compute`` falls back to them.
    """

    code_version: str
    scenario_id: NotRequired[str | None]
    aoi_id: NotRequired[str | None]


def execution_mode() -> ExecutionMode:
    """Whether this process may execute durable workflows.

    Production web processes are clients by default: they submit, read, cancel,
    and stream, but do not execute PostGIS. Workers explicitly opt into
    ``executor``. ``dev`` is deliberately explicit so local CLI and integration
    tests can retain one-process execution without making it a production
    deployment accident.
    """
    value = os.environ.get("REIMAGINA_DBOS_EXECUTION_MODE", "client")
    if value not in {"client", "executor", "dev"}:
        raise RuntimeError(
            "REIMAGINA_DBOS_EXECUTION_MODE must be one of: client, executor, dev"
        )
    return value  # type: ignore[return-value]


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
    """Identify this process. It must be **stable across restarts and unique
    between concurrent processes**, and getting either wrong is silent.

    Recovery is scoped by executor id: at launch a process reclaims the
    workflows recorded against its own id and no others.

    - Not stable → a restarted process has a new identity and reclaims
      *nothing*. Its predecessor's in-flight runs are orphaned with no error,
      and durable execution quietly stops being durable. A pid-derived id does
      exactly this, which is what this used to be.
    - Not unique → a starting process reclaims work a live peer is still
      running. On dbos 2.28.0 recovery is not idempotent, so that is a double
      execution — worse than the failure it was trying to repair.

    Nothing derivable satisfies both for every topology, so the default assumes
    the common one — at most one process per role per host — and the role keeps
    an API and a worker on the same host distinct. Beyond that, set
    ``REIMAGINA_EXECUTOR_ID`` per replica; a StatefulSet pod name or a systemd
    instance name is the right shape.
    """
    explicit = os.environ.get("REIMAGINA_EXECUTOR_ID")
    if explicit:
        return explicit
    role = os.environ.get("REIMAGINA_EXECUTOR_ROLE", "app")
    return f"{os.uname().nodename}:{role}"


def _assert_domain_database(dsn: str) -> None:
    """Refuse to launch against a database that is not ours.

    DBOS prefers making things work over failing: pointed at a database that
    does not exist it **creates one** and installs its schema, so a typo in
    ``DATABASE_URL_DIRECT`` produces a perfectly healthy-looking process whose
    checkpoints live somewhere nobody knows about. (Given no URL at all it falls
    back to a local SQLite file, which ``dbos_dsn`` already refuses.)

    Checking for a domain table catches all of it at startup — wrong DSN,
    unmigrated database, or the right host and the wrong environment — instead
    of at the first submit, when a run is already in flight.
    """
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('jobs_runtime.job_run')")
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                f"{dsn.rsplit('@', 1)[-1]} is not a migrated reimagina database "
                "(jobs_runtime.job_run is missing). DBOS creates a database "
                "rather than failing when one is absent, so a typo here would "
                "otherwise start cleanly and write checkpoints nowhere useful. "
                "Check DATABASE_URL_DIRECT, and run `make migrate`."
            )


def assert_same_domain_database(conn) -> None:
    """Ensure the planner and DBOS checkpoints share one database.

    The API's application DSN may use a pooled, least-privilege role while
    DBOS needs a direct connection. Different roles are fine; different
    databases are catastrophic: planning reads one environment and workers
    execute the durable workflow in another. Compare the server-side database
    identity rather than DSN text, because PgBouncer and URLs with different
    credentials are normal.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        caller_database = cur.fetchone()[0]
    with (
        contextlib.closing(psycopg2.connect(dbos_dsn())) as direct,
        direct.cursor() as cur,
    ):
        cur.execute("SELECT current_database(), to_regclass('jobs_runtime.job_run')")
        direct_database, domain_table = cur.fetchone()
    if domain_table is None:
        raise RuntimeError(
            "DATABASE_URL_DIRECT does not point at a migrated reimagina database "
            "(jobs_runtime.job_run is missing)"
        )
    if caller_database != direct_database:
        raise RuntimeError(
            "the submit connection and DATABASE_URL_DIRECT name different "
            f"databases ({caller_database!r} != {direct_database!r}); refusing "
            "to plan in one database and execute in another"
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


def ensure_system_database(dsn: str | None = None) -> None:
    """Install DBOS's system schema. Idempotent, and launches no executor.

    **Why this is not something `launch()` already covers.** It is, for a
    worker — and only for a worker. ``DBOS.launch()`` creates the ``dbos``
    schema as a side effect of starting an executor, so the schema exists from
    the first moment a worker runs and never before. Every other process in
    this system submits through ``DBOSClient``, whose own docstring states the
    precondition plainly: "Unlike DBOS itself, the client never runs schema
    migrations: the system database must already have been created by a DBOS
    application."

    Nothing in the deploy path satisfied that. `make migrate` ran Alembic,
    seeded the indicator catalog and the asset registry, and stopped — so a
    freshly-installed system served an Ops dashboard listing all eleven
    indicators whose Run button answered 500, ``relation
    "dbos.workflow_status" does not exist``, until somebody happened to start
    a worker. The ordering dependency was real, undocumented, and invisible
    until the first submit.

    So the schema becomes a bootstrap artifact like any other, created by the
    same command that creates the rest of the database. A worker launching
    later finds it already current and skips the work; `run_migrations` takes
    an advisory lock and short-circuits when the recorded version is the
    latest, so concurrent callers and repeat deploys are both fine.

    ⚠ Wants the **direct** DSN. The migration takes a *session* advisory lock
    on an AUTOCOMMIT connection to serialise concurrent deploys, and installs
    the LISTEN/NOTIFY triggers the progress stream reads — neither of which
    survives a transaction-mode pooler, where consecutive statements can land
    on different backends, so the lock covers none of the DDL that follows it
    and is never released by the backend holding it. This is the same reason
    `dbos_dsn` refuses anything else, and `.env.example` says the same of
    Alembic.

    ⚠ Deliberately **not** implemented by launching DBOS and immediately
    destroying it. ``launch()`` also starts the queue dequeuer and the
    startup-recovery thread, and recovery reclaims workflows recorded against
    this executor id — so a bootstrap command would begin *executing PostGIS
    workloads*, in the deploy process, as a side effect of creating a schema.
    Provisioning and executing have to stay separable, which is the same
    distinction `execution_mode()` draws.

    ⚠ ``SystemDatabase`` is DBOS-internal. It is reached directly because the
    public surface offers no provision-without-executing entry point, and the
    dependency is pinned (``dbos>=2.28,<3``) for unrelated reasons that happen
    to bound this one too. `tests/integration/test_dbos_system_schema_bootstrap`
    asserts the schema really appears, so an upstream move breaks a test rather
    than quietly reverting us to a 500 on first submit.
    """
    from dbos._serialization import DefaultSerializer
    from dbos._sys_db import SystemDatabase

    dsn = dsn or dbos_dsn()
    # The same guard `launch()` applies, for the same reason and more sharply
    # here: pointed at a database that does not exist, DBOS creates one and
    # installs this very schema into it. A bootstrap command is exactly where
    # a DSN typo gets typed, and "created the ops plane somewhere else and
    # said OK" is the one outcome that must not be possible.
    _assert_domain_database(dsn)
    system_db = SystemDatabase.create(
        system_database_url=dsn,
        engine_kwargs={
            "connect_args": {"application_name": "reimagina_bootstrap"},
            "pool_size": 2,
            "max_overflow": 0,
            "pool_timeout": 30,
            "pool_pre_ping": True,
        },
        engine=None,
        schema="dbos",
        serializer=DefaultSerializer(),
        executor_id=None,
        # Must match what `config()` leaves DBOS defaulting to: the migration
        # installs the LISTEN/NOTIFY triggers the progress stream reads, and a
        # system database created without them degrades the SSE endpoint to
        # once-a-second polling — the exact latency it exists to avoid.
        use_listen_notify=True,
    )
    try:
        system_db.run_migrations()
    finally:
        system_db.destroy()


_runs_queue: Queue | None = None
_units_queue: Queue | None = None
_LAUNCH_LOCK = threading.Lock()


def launch() -> None:
    """Launch an executor and bind the run and unit queues. Idempotent.

    Locked, because the check and the assignment are not atomic and both the
    API and the CLI call this from a request path. ``DBOS.launch()`` marks
    itself launched *before* it finishes building the system database, so a
    second thread arriving mid-launch would get "already launched", then fail
    in ``register_queue`` with "System database accessed before DBOS was
    launched" — turning one concurrent submit into a 500.
    """
    global _runs_queue, _units_queue
    if _runs_queue is not None and _units_queue is not None:
        return
    with _LAUNCH_LOCK:
        if _runs_queue is not None and _units_queue is not None:
            return
        _assert_domain_database(dbos_dsn())
        cfg = config()
        if not os.environ.get("REIMAGINA_EXECUTOR_ID"):
            logger.warning(
                "REIMAGINA_EXECUTOR_ID is unset; using %r. Crash recovery only "
                "reclaims work recorded against this exact id, so it must be "
                "stable across restarts and unique between concurrent "
                "processes. Set it per replica when running more than one "
                "process per role on a host.",
                cfg["executor_id"],
            )
        DBOS(config=cfg)
        DBOS.launch()
        # After launch, per the deprecation of constructing Queue() directly.
        # worker_concurrency, not concurrency: the global limit also counts
        # PENDING workflows from earlier application versions, which a
        # blue-green deploy deliberately leaves lying around.
        _runs_queue = DBOS.register_queue(
            RUNS_QUEUE,
            worker_concurrency=int(os.environ.get("REIMAGINA_RUN_CONCURRENCY", "2")),
            polling_interval_sec=POLL_INTERVAL_SEC,
        )
        # 8 units per worker, measured rather than guessed. On the real catalog
        # (13 indicators, 1500 blocks, 3 reps), as-completed dispatch:
        #
        #   slots   makespan   busy   parallelism   speedup   efficiency
        #   1         6605ms   5138          0.81      1.00         1.00
        #   2         3614ms   5122          1.55      1.83         0.91
        #   4         2181ms   5190          2.73      3.03         0.76
        #   8         1691ms   5274          3.92      3.91         0.49   <- chosen
        #   16        1629ms   5416          4.03      4.06         0.25
        #
        # This was 2, which cost 2.1x. The ceiling is the graph, not the engine:
        # a DAG's average parallelism is work/span, here 5416/1344 = 4.03, and at
        # 16 slots we measure exactly 4.03. Makespan 1629ms against a 1344ms
        # critical path is an SLR of 1.21 — near the lower bound for this shape.
        # So 16 buys 3.7% over 8 and there is nothing beyond it to buy.
        #
        # ⚠ 8 is the *latency* choice, at 0.49 efficiency: eight workers for a
        # 3.9x speedup. Lower it toward 4 (0.76) where many runs execute
        # concurrently and throughput matters more than one run's wall clock.
        #
        # ⚠ `busy` climbing 5138 -> 5416ms across the sweep is real contention on
        # the one shared Postgres — the same work taking longer. Mild here, and
        # the thing to watch first if this is raised again: it, not the
        # scheduler, is what bounds slot count at city scale.
        _units_queue = DBOS.register_queue(
            UNITS_QUEUE,
            worker_concurrency=settings.unit_concurrency(),
            polling_interval_sec=POLL_INTERVAL_SEC,
        )


def runs_queue() -> Queue:
    if _runs_queue is None:
        raise RuntimeError("dbos_app.launch() must run before a run is submitted")
    return _runs_queue


def units_queue() -> Queue:
    if _units_queue is None:
        raise RuntimeError("dbos_app.launch() must run before a run is submitted")
    return _units_queue


@contextmanager
def _pg():
    """A short-lived autocommit connection that is actually closed on exit.

    ``with psycopg2.connect(...) as conn`` scopes the *transaction*, not the
    connection: it commits or rolls back and leaves the socket open. Every step
    below used that idiom, so each leaked a connection per call — and the steps
    run several times per unit, per run.
    """
    conn = psycopg2.connect(dbos_dsn())
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


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


def validate_workflow_input(units: Sequence[UnitInput], params: RunParams) -> None:
    """Reject malformed persisted inputs before replay can make them opaque."""
    seen: set[str] = set()
    for unit in units:
        slug = unit.get("slug")
        features = unit.get("features")
        deps = unit.get("deps")
        if not isinstance(slug, str) or not slug:
            raise ValueError("workflow unit slug must be a non-empty string")
        if slug in seen:
            raise ValueError(f"workflow contains duplicate unit {slug!r}")
        if not isinstance(features, list) or not all(
            isinstance(f, str) for f in features
        ):
            raise ValueError(f"workflow unit {slug!r} has invalid feature ids")
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            raise ValueError(f"workflow unit {slug!r} has invalid dependencies")
        seen.add(slug)

    # ``plan_wave`` intentionally ignores dependencies outside this dirty set:
    # their current values are inputs, not work in this run. Within the set a
    # cycle can never make progress, so reject it before persisting a workflow
    # which would otherwise remain queued forever.
    try:
        topological_order({u["slug"]: u["deps"] for u in units}, nodes=seen)
    except CycleError as exc:
        raise ValueError(f"workflow dependency cycle: {exc.args[1]!r}") from exc
    if not isinstance(params.get("code_version"), str) or not params["code_version"]:
        raise ValueError("workflow params require a non-empty code_version")


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
        # ⚠ Explicit kwargs, not `**params`. The splat coupled this call to the
        # exact shape of `RunParams`, so adding a field there — which its own
        # docstring invites — raised `TypeError: create_run() got an unexpected
        # keyword argument`. Adding `plan_id` did exactly that, and the run
        # failed inside the workflow's first step rather than at submit.
        runstore.create_run(
            conn,
            run_id=run_id,
            status="running",
            code_version=params["code_version"],
            scenario_id=params.get("scenario_id"),
            aoi_id=params.get("aoi_id"),
        )
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
def s_settle_unit(run_id: str, slug: str, outcome: str) -> str:
    """Force a unit terminal and return the status the database actually holds.

    Two jobs, both needed.

    It reconciles. Only the child writes a unit's status, so a child whose
    process died — and whose recovery attempts ran out — leaves ``job_unit``
    stuck at ``running`` forever, while the parent sees the failure and closes
    the run. ``GET /jobs/{id}`` would then report a terminal run containing a
    running unit, contradicting the progress stream, with nothing to fix it.

    And it makes the outcome checkpointed. The parent branches on the return
    value, not on ``get_result``, so a replay reads a recorded status rather
    than re-querying a child whose live status may since have moved.
    """
    with _pg() as conn:
        runstore.set_unit_status(
            conn, run_id=run_id, indicator_slug=slug, status=outcome
        )
        rows = {u["indicator"]: u["status"] for u in runstore.list_units(conn, run_id)}
    # The store refuses to move a unit out of a terminal state, so what it holds
    # wins over what we asked for — a cancel that landed first must survive.
    return rows.get(slug, outcome)


@DBOS.step()
def s_release_plan(plan_id: str) -> int:
    """Reclaim the run's plan rows once it is terminal.

    ⚠ A *new step, appended at the end*. DBOS matches replayed steps by position,
    so inserting one earlier would shift every later position and hand one
    indicator's checkpointed output to another (spike 2). Appending leaves
    positions 0..N untouched: a resumed workflow replays them from checkpoints
    and executes this one fresh.

    Best-effort. Anything missed — a crash, a cancel before the finalizer, an
    idempotency collision — is reclaimed by the TTL sweep at the next submit.
    """
    if not plan_id:
        return 0
    with _pg() as conn:
        return backend_for_connection(conn).run_plan_repo().delete(plan_id=plan_id)


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

    **One transaction, committed at the end.** The write path is
    stale-then-insert: it flips the previous values to ``status='stale'`` and
    then inserts replacements. On an autocommit connection those are two
    transactions, so a ``pg_cancel_backend`` landing between them left those
    features with *zero* current rows — and because the workflow is cancelled,
    recovery never re-runs the step to repair it. ``serving_geo`` exposes only
    ``current``, so that was a permanent hole in served values. In one
    transaction, a cancel mid-write rolls the flip back with it.

    **Cancellation is re-checked here.** ``cancel_run`` cannot signal a unit
    that has not yet opened its ``job_run`` row — no row, no backend pid — and
    DBOS's own check already fired when this step began. Without this the unit
    would run its full compute and commit results for a run already reported
    terminal. A narrowing, not a closing: a cancel arriving after this check
    still has only ``pg_cancel_backend``, which is what the row exists for.
    """
    runner = default_runners().get(slug)
    if runner is None:
        raise LookupError(f"no runner registered for {slug!r}")

    dsn = dbos_dsn()
    conn = psycopg2.connect(dsn)  # NOT autocommit — see above
    started_at = datetime.now(timezone.utc)
    try:
        row = runstore.get_run(conn, run_id)
        if row is not None and row["status"] in RUN_TERMINAL_STATES:
            logger.info(
                "skipping %s for run %s: the run is already %s",
                slug,
                run_id,
                row["status"],
            )
            return
        # ⚠ Read from `jobs_runtime.run_plan`, not from the workflow input. The
        # ids used to travel inside that input, one per feature per indicator,
        # which made the payload scale with the size of the city; `plan_id` is a
        # handle of fixed size. `features` is the pre-`plan_id` shape, kept for
        # one release so a run submitted before this change and resumed after it
        # still finds its work — a current submitter always sends it empty.
        plan_id = params.get("plan_id")
        resolved = (
            backend_for_connection(conn)
            .run_plan_repo()
            .features_for(plan_id=plan_id, indicator_slug=slug)
            if plan_id
            else features
        )
        with current_run(run_id):
            runner(
                conn,
                features=frozenset(resolved),
                scenario_id=params.get("scenario_id"),
                code_version=params.get("code_version", "dev"),
                aoi_id=params.get("aoi_id"),
            )
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        # That rollback just erased the `failed` job_run the runner opened — it
        # was written on this connection. Re-record it on its own, or a unit
        # that burned minutes and failed leaves no provenance at all.
        #
        # ⚠ `dsn`, NOT `conn.dsn`. psycopg2 returns a live connection's DSN with
        # the password replaced by the literal `xxx`, so reconnecting from it
        # fails authentication against any database that has one — and the
        # best-effort `except` inside swallows that into a log line, so failed
        # units simply stop leaving provenance. The string this connection was
        # opened with is the same database by construction, and works.
        record_failed_job_run(
            dsn,
            run_id=run_id,
            indicator_slug=slug,
            scenario_id=params.get("scenario_id"),
            code_version=params.get("code_version", "dev"),
            error=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
        )
        raise
    finally:
        conn.close()


# ── Workflows ───────────────────────────────────────────────────────────────


@DBOS.workflow()
def unit_workflow(run_id: str, slug: str, features: list[str], params: dict) -> str:
    """Compute one indicator.

    ⚠ Arguments are **append-only**. DBOS stores them positionally, so a child
    persisted with N arguments replays into this function by position: inserting
    or reordering a parameter makes every in-flight child fail on replay with a
    TypeError, surfacing as an unrecoverable run. Removing one is the same
    hazard in reverse — a child persisted with more arguments than this accepts
    fails the same way, which is why dropping the old trailing ``produces``
    parameter required draining first.
    """
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
    """Drive the dirty-set to completion, dispatching each unit as it unblocks.

    ``units`` is a workflow *input*, so the loop below is reproducible on
    replay no matter what the catalog does meanwhile. Everything the body
    branches on is either that input or a checkpointed step result.

    **As-completed, not level-synchronous.** A unit starts the moment its last
    dependency clears, rather than waiting for the slowest member of its level.
    ``perf/workloads.py``'s ``barrier_probe`` exists to measure the difference,
    and ``head_of_line_ms`` is the metric that reports it.

    ⚠ **Why this is replay-safe, and why the obvious version is not.** DBOS
    resolves a replayed child by ``(parent_workflow_id, function_id)`` *only*
    (``_sys_db.py``), and ``DBOSUnexpectedStepError`` fires only when the
    function *name* at that slot differs. Every child here is ``unit_workflow``,
    so the names always match: a body whose step order depends on which child
    finished first does **not** fail loudly on replay, it silently receives a
    handle to the wrong child and cross-attributes one indicator's result to
    another. That is the spike 2 failure mode.

    ``DBOS.wait_first`` is what makes this sound. It runs its poll through
    ``call_function_as_step(..., "DBOS.waitFirst", ...)``, so **the completion
    order itself becomes durable history** — a replay returns the recorded
    winner instead of re-racing. Every value this loop branches on is therefore
    checkpointed: ``plan_wave`` is pure over ``statuses``, ``statuses`` comes
    from ``s_settle_unit``'s recorded *return*, and ``outstanding`` moves only on
    a dispatch or a recorded winner. The function ids consumed per iteration are
    ``2*len(skip) + len(dispatch) + 4``, every term fixed by that history.

    ``outcome`` may be recomputed differently on replay — ``get_result`` is
    recorded but never verified, so it re-queries the child's *live* status —
    and that is harmless only because it is an **argument, never a branch**.
    ``statuses`` takes what the step returns, not what was passed to it.

    Polling ``get_status`` or ``DBOS.sleep`` in a loop would work here and must
    not be used: each consumes a function id, so the slot count would become a
    function of wall-clock timing.
    """
    s_open_run(run_id, units, params)

    registry = _as_registry(units)
    order = [u["slug"] for u in units]
    features = {u["slug"]: u["features"] for u in units}
    statuses = {slug: "queued" for slug in order}
    outstanding: dict[str, str] = {}  # child workflow id -> slug

    while True:
        # `registry` is `_Spec`, a two-field stand-in carrying only what
        # `plan_wave` reads. Structural, not nominal — the workflow input is
        # the source of truth here, never the catalog.
        wave = plan_wave(
            [(slug, statuses[slug]) for slug in order],
            registry,  # type: ignore[arg-type]
        )

        for slug in wave.skip:
            # Announce only what the database accepted, exactly as the settled
            # path does. A cancel that already closed this unit must not be
            # overwritten on the wire by a `skipped` the store refused.
            statuses[slug] = s_settle_unit(run_id, slug, "skipped")
            _emit(run_id, slug, statuses[slug], "unsatisfied input")

        # `wave.dispatch` preserves the order of `units`, which
        # `compute_dirty_set` sorts explicitly because an unsorted set made it
        # vary with PYTHONHASHSEED.
        for slug in wave.dispatch:
            child_id = f"{run_id}-{slug}"
            with SetWorkflowID(child_id):
                handle = units_queue().enqueue(
                    unit_workflow, run_id, slug, features[slug], params
                )
            # ⚠ The tripwire. An explicit id does NOT free a child from
            # positional replay — the function_id counter increments regardless,
            # and DBOS discards this id if the slot already records another. It
            # buys two things instead. A re-enqueue past recorded history dedups
            # on the id rather than minting a second child, which is the
            # duplicate-compute `s_compute(retries_allowed=False)` exists to
            # prevent. And comparing the handle back against the id we asked for
            # turns the silent cross-wiring described above into an immediate,
            # local failure — the only detection available, since DBOS itself
            # cannot see it.
            if handle.workflow_id != child_id:
                raise RuntimeError(
                    f"replay divergence: step position bound to "
                    f"{handle.workflow_id!r}, expected {child_id!r}"
                )
            outstanding[child_id] = slug
            # Local only, so the next `plan_wave` does not re-dispatch it. The
            # database's own `running` is written by the child.
            statuses[slug] = "running"

        if not outstanding:
            # Nothing is running — but that is not the same as nothing left to
            # do. `plan_wave` recomputes readiness from the statuses it is
            # handed, so it marks exactly **one layer** of skips per call: with
            # `a -> b -> c` and `a` failed, this pass skips `b`, and only the
            # next pass can see `b` as terminal and skip `c`. Breaking here
            # would strand `c` at `queued` forever, on a run the API reports as
            # finished — `overall_status` returns `failed` either way, so the
            # run looks correct while one indicator silently never resolves.
            #
            # Looping while skips are still landing terminates: each pass moves
            # at least one unit from `queued` to `skipped`, and the set is
            # finite. Guarded by tests/unit/test_workflow_position_identity.py.
            if not wave.skip:
                break
            continue

        # `retrieve_workflow(existing_workflow=False)` costs no function id and
        # touches no database — it just wraps an id. Rebuilding the handles this
        # way each iteration keeps the set a pure function of `outstanding`,
        # which matters because `wait_first` looks its recorded winner up in this
        # exact list and raises a bare KeyError if it is missing.
        handles: list[Any] = [
            DBOS.retrieve_workflow(child_id, existing_workflow=False)
            for child_id in sorted(outstanding)
        ]
        winner = DBOS.wait_first(handles, polling_interval_sec=POLL_INTERVAL_SEC)
        slug = outstanding.pop(winner.workflow_id)
        try:
            winner.get_result(polling_interval_sec=POLL_INTERVAL_SEC)
            outcome = "succeeded"
        except Exception:  # noqa: BLE001 — fault isolation is the point
            outcome = "failed"
        statuses[slug] = s_settle_unit(run_id, slug, outcome)
        _emit(run_id, slug, statuses[slug])

    final = overall_status(statuses.values())
    s_finalize(run_id, final)
    # After the verdict, so a failure here cannot change it.
    s_release_plan(params.get("plan_id", ""))
    _emit(run_id, None, final)
    DBOS.close_stream(PROGRESS_STREAM)
    return final
