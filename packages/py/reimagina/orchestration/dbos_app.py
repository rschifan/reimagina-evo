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
from typing import Literal, NotRequired, TypedDict

import psycopg2
from dbos import DBOS, DBOSConfig, Queue

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
        )
        _units_queue = DBOS.register_queue(
            UNITS_QUEUE,
            worker_concurrency=int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "2")),
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
            # Announce only what the database accepted, exactly as the settled
            # path does. A cancel that already closed this unit must not be
            # overwritten on the wire by a `skipped` the store refused.
            statuses[slug] = s_settle_unit(run_id, slug, "skipped")
            _emit(run_id, slug, statuses[slug], "unsatisfied input")

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
                outcome = "succeeded"
            except Exception:  # noqa: BLE001 — fault isolation is the point
                outcome = "failed"
            # Settle through a step, and branch on what the step returns.
            #
            # `get_result` looks checkpointed but is not: DBOS records it and
            # never reads it back, so a replay re-queries the child's *live*
            # status. If a child were resumed to SUCCESS after the parent had
            # already skipped its dependents, the replayed body would take the
            # dispatch branch instead of the skip branch and the step positions
            # would diverge — DBOSUnexpectedStepError, run unrecoverable.
            # A step's output is genuinely checkpointed, so branching on that
            # keeps the body reproducible.
            statuses[slug] = s_settle_unit(run_id, slug, outcome)
            _emit(run_id, slug, statuses[slug])

    final = overall_status(statuses.values())
    s_finalize(run_id, final)
    _emit(run_id, None, final)
    DBOS.close_stream(PROGRESS_STREAM)
    return final
