"""Workload-suite harness — Track B (Hatchet, engine-owned scheduling).

This file is the half of the suite that **differs between the two ops-plane
worktrees**, and the differences are the comparison rather than incidental:

* Track B needs a standing worker; Track A starts none, because a DBOS submitter
  drives its own run in-process.
* Track B declares one engine workflow per workload, because its DAG is a
  deployed artifact built at import from a registry. Track A passes the DAG as
  workflow *input*, so a new shape costs it a new orchestrator instance.
* Track B's task body resolves runners from the registry it closed over, so
  installing synthetic computes into ``_DIRECT_COMPUTE`` is enough. Track A's
  step calls ``default_runners()`` with no argument and needs a second patch.

Everything in ``reimagina.perf.workloads`` / ``workload_report`` is shared and
byte-identical; only this harness diverges.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

import psycopg2
import pytest

from reimagina.orchestration import runners
from reimagina.orchestration.port import RUN_TERMINAL_STATES
from reimagina.perf.workload_report import db_now, unit_records

_LOG = logging.getLogger(__name__)

TRACK = "hatchet"

_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://localhost/reimagina_test_bench_hatchet"
)
"""⚠ A database of this track's own, not the shared conformance one.

The two tracks' migration histories fork — r032 on the library track, r033 here,
both off r031 and mutually unresolvable. A database migrated by one worktree
makes the other's alembic fail outright with "Can't locate revision". Sharing one
is not slow, it is impossible.
"""

SLOTS = int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "4"))
WORKER_NAME = "workload-bench"
_SETTLE_TIMEOUT = 180.0
_POLL_S = 0.025
"""Poll interval on `get_run`. It lands inside `settle_lag_ms`, so it is
recorded in the report rather than left for a reader to guess."""


def reps() -> int:
    """How many times to run each workload; the first is discarded as warm-up.

    A single timing is not a measurement, and warm-up discard is not optional:
    the conformance suite's convergence budget is explicitly sized for the first
    submit of a session, because a freshly registered worker and freshly declared
    workflow make the engine do one-time bookkeeping.
    """
    explicit = os.environ.get("REIMAGINA_BENCH_REPS")
    if explicit:
        return max(1, int(explicit))
    return 1 if os.environ.get("REIMAGINA_BENCH_SCALE", "full") == "smoke" else 3


@dataclass(frozen=True)
class Implementation:
    mode: str
    blocking: bool
    """`submit_run` returns only once the run is terminal."""
    needs_worker: bool


IMPLEMENTATIONS = [
    Implementation(mode="sync", blocking=True, needs_worker=False),
    Implementation(mode="hatchet", blocking=False, needs_worker=True),
]
"""`sync` is not filler. It is T1 for `speedup`, and because it is the same code
on both worktrees its two columns must agree — which is the only check that
catches a harness measuring itself rather than the engines.
"""


def pytest_collection_modifyitems(config, items):
    """Refuse to run by accident.

    `testpaths = ["tests"]` means a bare `pytest` would collect this suite and
    spend minutes on it. Opt in explicitly.
    """
    if os.environ.get("REIMAGINA_BENCH") == "1":
        return
    skip = pytest.mark.skip(reason="workload suite: set REIMAGINA_BENCH=1 to run")
    for item in items:
        item.add_marker(skip)


# ── Database ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def bench_db() -> str:
    from alembic import command
    from alembic.config import Config

    if "test" not in _DSN.rsplit("/", 1)[-1]:
        pytest.fail(f"refusing to run against a non-test database: {_DSN}")
    try:
        psycopg2.connect(_DSN).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"bench database unavailable: {exc}")

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _DSN)
    command.upgrade(cfg, "head")
    # ⚠ No `downgrade base` on teardown, unlike tests/conftest.py. Re-migrating
    # 33 revisions per session costs more than the suite itself, and the bench
    # database exists for nothing else.
    return _DSN


@pytest.fixture
def conn(bench_db):
    """One connection per test, used for submit *and* for reading results back.

    Must be the same connection for both: `sync` runs every unit in the caller's
    transaction and never commits, so its `job_run` rows are visible only here.
    The engine-backed modes commit on their own connections, so they are visible
    from anywhere — reading on this one works for all three.
    """
    connection = psycopg2.connect(_DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture(scope="session", autouse=True)
def _sweep_bench_rows(bench_db):
    """Delete rows this suite wrote, on the way *in*.

    On the way in rather than out, for the same reason the worker reaping is:
    a killed run leaves rows behind and teardown never gets to argue about it.
    """
    with psycopg2.connect(_DSN) as scrub:
        scrub.autocommit = True
        with scrub.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.job_run WHERE code_version LIKE 'bench-%'"
            )
    yield


# ── The engine side ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def bench_workflows():
    """One engine workflow per workload, under a stable, distinct name.

    ⚠ Distinct names, not one reshaped workflow. What the engine does with a
    changed definition under an existing name is not established (spike B9) —
    whether it versions, overwrites or rejects — and a benchmark is the worst
    possible place to find out. Stable names also mean re-running the suite
    re-registers rather than accumulates.

    This is the concrete cost of an engine-owned DAG: every shape has to exist as
    a deployed artifact before anything can be submitted to it. The library track
    needs none of this — it hands the DAG over as workflow input at submit time.
    """
    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.skip("HATCHET_CLIENT_TOKEN unset — no engine to register with")

    from reimagina.orchestration import hatchet_app
    from reimagina.perf.workloads import WORKLOADS

    built = {}
    for workload in WORKLOADS:
        workflow, _nodes = hatchet_app.build_dag(
            workload.registry, name=f"reimagina-bench-{workload.id}"
        )
        built[workload.id] = workflow
    return built


@pytest.fixture(scope="session")
def bench_worker(bench_workflows, bench_db):
    """One in-process worker serving every bench workflow for the session.

    In-process and in a daemon thread on purpose: the synthetic computes are
    installed into ``runners._DIRECT_COMPUTE`` by a function-scoped fixture, and
    the task body resolves them at call time, so a same-process worker sees them
    and a subprocess would not.
    """
    from reimagina.orchestration import hatchet_app

    os.environ.setdefault("DATABASE_URL_DIRECT", _DSN)
    _reap_stale_workers()

    worker = hatchet_app.hatchet.worker(
        WORKER_NAME, slots=SLOTS, workflows=list(bench_workflows.values())
    )
    thread = threading.Thread(target=worker.start, daemon=True)
    thread.start()
    _await_registration(set(f"reimagina-bench-{w}" for w in bench_workflows))
    yield worker

    _stop_worker(worker)


def _stop_worker(worker, timeout: float = 20.0) -> None:
    """Actually stop the worker, and make sure the session can exit.

    ⚠ Two SDK facts make the obvious `worker.exit_gracefully()` wrong, and both
    were measured here rather than read:

    1. **`exit_gracefully` is `async def`.** Calling it from sync code returns a
       coroutine that is never awaited — Python only warns — so it does nothing
       at all. The conformance suite's teardown makes exactly this call, which
       is very likely why that suite's workers were observed leaking despite a
       "clean" shutdown. It has to be driven on the worker's own loop.

    2. **The action listener is a spawned child process, not a thread**
       (`multiprocessing.get_context("spawn")`), and it is not a daemon. Python
       joins non-daemon children at interpreter exit, so a listener that never
       stops hangs the *whole pytest session* after the last test has passed —
       a hang with no output and no failing test, which is precisely what this
       looked like before it was diagnosed.

    So: drive the coroutine on the worker's loop, then verify the child is gone
    and terminate it if not. Belt and braces on purpose — a benchmark harness
    that wedges the session is worse than one that stops a listener rudely.
    """
    loop = getattr(worker, "_loop", None)
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                worker.exit_gracefully(), loop
            ).result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — teardown must not mask a failure
            _LOG.warning("graceful worker stop failed: %s", exc)

    for attr in ("_action_listener_process", "_durable_action_listener_process"):
        proc = getattr(worker, attr, None)
        if proc is not None and proc.is_alive():
            _LOG.warning("%s survived graceful stop; terminating", attr)
            proc.terminate()
            proc.join(timeout=5.0)


def _reap_stale_workers() -> None:
    """Pause every bench worker the engine still believes in.

    Worker registrations live in the engine's database, survive the process that
    made them, survive an engine restart, and are never removed — `pause` is the
    only verb there is. `exit_gracefully()` stops the local loop and deregisters
    nothing. Left alone the engine keeps handing work to processes that no longer
    exist, and the tell is not a failure but suite wall clock drifting run over
    run (62s → 322s was measured on unchanged code). Only one live bench worker
    exists at a time and it has not started yet, so everything found here is stale.
    """
    from reimagina.orchestration import hatchet_app

    try:
        listed = hatchet_app.hatchet.workers.list()
    except Exception as exc:  # noqa: BLE001 — reachability is checked elsewhere
        _LOG.warning("could not list workers to reap: %s", exc)
        return

    for row in getattr(listed, "rows", None) or []:
        if getattr(row, "name", None) != WORKER_NAME:
            continue
        worker_id = getattr(getattr(row, "metadata", None), "id", None)
        if not worker_id:
            continue
        try:
            hatchet_app.hatchet.workers.pause(worker_id)
            _LOG.info("paused stale bench worker %s", worker_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("could not pause stale worker %s: %s", worker_id, exc)


def _await_registration(expected: set[str], timeout: float = 60.0) -> None:
    from reimagina.orchestration import hatchet_app

    deadline = time.monotonic() + timeout
    last: object = "no response"
    while time.monotonic() < deadline:
        try:
            listed = hatchet_app.hatchet.workflows.list()
            names = {
                getattr(w, "name", None) for w in (getattr(listed, "rows", None) or [])
            }
            if expected <= names:
                return
            last = f"missing: {sorted(expected - names)}"
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    pytest.skip(f"bench workflows did not register within {timeout}s: {last}")


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda impl: impl.mode)
def impl(request, bench_db, monkeypatch) -> Implementation:
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    monkeypatch.setenv("REIMAGINA_UNIT_CONCURRENCY", str(SLOTS))
    if request.param.needs_worker:
        request.getfixturevalue("bench_worker")
    return request.param


# ── Installing a workload ───────────────────────────────────────────────────


@pytest.fixture
def install(monkeypatch, request):
    """Bind a workload's synthetic computes, and point the submit path at it.

    ``monkeypatch.setitem`` on ``_DIRECT_COMPUTE`` is the same seam the
    conformance `poison` fixture uses, and it works across the worker thread for
    the same reason: ``default_runners`` reads the dict at call time inside the
    task body.
    """

    def _install(workload, impl: Implementation):
        for slug, compute in workload.computes.items():
            monkeypatch.setitem(runners._DIRECT_COMPUTE, slug, compute)

        if impl.mode == "sync":
            from reimagina.orchestration.synchronous import SynchronousOrchestrator

            return SynchronousOrchestrator(registry=workload.registry)

        from reimagina.orchestration import hatchet_app
        from reimagina.orchestration.hatchet_orchestrator import HatchetOrchestrator

        # `submit_run` dereferences `hatchet_app.run_workflow` at call time, so
        # pointing the module attribute at this workload's workflow is what
        # routes the submit. The worker already has every bench workflow
        # registered, so nothing on the engine side has to change.
        workflows = request.getfixturevalue("bench_workflows")
        monkeypatch.setattr(hatchet_app, "run_workflow", workflows[workload.id])
        return HatchetOrchestrator(registry=workload.registry)

    return _install


# ── Running one repetition ──────────────────────────────────────────────────


def run_once(orchestrator, conn, workload, impl: Implementation) -> dict:
    """Submit, wait for terminal, and read the run back. One repetition.

    Timestamps come from the Postgres clock, not Python's, so that
    `dispatch_lead_ms` and the parent/child ordering check are exact against the
    `job_run` rows rather than approximately right.
    """
    # ⚠ A fresh code_version per submit, or the engine's idempotency window
    # (TTL 60s, keyed on the work's fingerprint) folds repetitions 2 and 3 onto
    # repetition 1 and the suite measures one run three times. It also keeps the
    # concurrency key unique, so reps do not cancel each other under
    # CANCEL_NEWEST.
    code_version = f"bench-{uuid4().hex[:8]}"

    t_submit = db_now(conn)
    handle = orchestrator.submit_run(
        conn,
        changed_attributes=set(workload.changed_attributes),
        edited_features={_BENCH_FEATURE},
        code_version=code_version,
    )

    if impl.blocking:
        status = handle.status
        t_terminal = db_now(conn)
    else:
        status, t_terminal = _await_terminal(orchestrator, conn, handle.run_id)

    records = unit_records(conn, handle.run_id)
    return {
        "run_id": handle.run_id,
        "code_version": code_version,
        "status": status.status if status else None,
        "unit_statuses": {u.indicator: u.status for u in (status.units if status else ())},
        "records": records,
        "t_submit": t_submit,
        "t_terminal": t_terminal,
    }


def _await_terminal(orchestrator, conn, run_id):
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        # ⚠ Roll back between polls. `get_run` on an engine-backed track still
        # touches this connection, and a transaction opened at the first poll
        # would hold a snapshot from before the units committed — so the reads
        # afterwards would never see them.
        conn.rollback()
        last = orchestrator.get_run(conn, run_id)
        if last is not None and last.status in RUN_TERMINAL_STATES:
            return last, db_now(conn)
        time.sleep(_POLL_S)
    raise AssertionError(
        f"run {run_id} did not settle within {_SETTLE_TIMEOUT}s "
        f"(last status: {last.status if last else None})"
    )


_BENCH_FEATURE = "00000000-0000-4000-8000-0000000000b1"


# ── Provenance ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def provenance(bench_db) -> dict:
    """What would make two reports incomparable, recorded so the diff can say so."""
    with psycopg2.connect(_DSN) as probe, probe.cursor() as cur:
        cur.execute("SHOW server_version")
        pg_version = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_conns = cur.fetchone()[0]

    return {
        "track": TRACK,
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "postgres_version": pg_version,
        "postgres_max_connections": int(max_conns),
        "slots": SLOTS,
        "scale": os.environ.get("REIMAGINA_BENCH_SCALE", "full"),
        "reps": reps(),
        "poll_interval_ms": _POLL_S * 1000,
        "engine_sdk": _version("hatchet-sdk"),
        "git_sha": _git_sha(),
        "retries_configured": int(os.environ.get("REIMAGINA_UNIT_RETRIES", "2")),
    }


def _version(package: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None
