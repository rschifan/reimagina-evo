"""Workload-suite harness — Track A (DBOS, we schedule).

This file is the half of the suite that **differs between the two ops-plane
worktrees**, and the differences are the comparison rather than incidental:

* Track A starts **no worker at all**. A DBOS submitter launches the library
  in-process and drives its own run, so there is nothing to register, nothing to
  reap, and no engine to be unavailable. The service track's harness needs a
  standing worker, a registration wait, and a stale-worker sweep — compare the
  two files.
* Track A declares no workflow per shape. The DAG is *workflow input*, so a new
  topology costs a new orchestrator instance and nothing more.
* ⚠ Track A needs one patch the service track does not: its compute step calls
  ``default_runners()`` with **no registry argument**, so it can only ever
  resolve ``SHIPPED_INDICATORS``. See `install`.

Everything in ``reimagina.perf.workloads`` / ``workload_report`` is shared and
byte-identical; only this harness diverges.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from uuid import uuid4

import psycopg2

from reimagina import settings
import pytest

from reimagina.orchestration import runners
from reimagina.orchestration.port import RUN_TERMINAL_STATES
from reimagina.perf.workload_report import db_now, unit_records

_LOG = logging.getLogger(__name__)

TRACK = "dbos"

_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://localhost/reimagina_test_bench_dbos"
)
"""⚠ A database of this track's own, not the shared conformance one.

The two tracks' migration histories fork — r032 here, r033 on the service track,
both off r031 and mutually unresolvable. A database migrated by one worktree
makes the other's alembic fail outright with "Can't locate revision". Sharing one
is not slow, it is impossible.
"""

SLOTS = settings.unit_concurrency()
_SETTLE_TIMEOUT = 180.0
_POLL_S = 0.025
"""Poll interval on `get_run`. It lands inside `settle_lag_ms`, so it is
recorded in the report rather than left for a reader to guess."""


def reps() -> int:
    """How many times to run each workload; the first is discarded as warm-up.

    A single timing is not a measurement. The warm-up matters less here than on
    the service track — there is no registration handshake — but it is discarded
    on both so the two reports are built the same way.
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
    Implementation(mode="dbos", blocking=False, needs_worker=False),
]
"""⚠ `needs_worker=False` on **both** rows, and that is the finding rather than a
convenience. The service track's equivalent list has it True for its engine mode,
because a submitted run there is not merely un-executed without a worker — it is
un-observable.

`sync` is also T1 for `speedup`, and because it is the same code on both
worktrees its two columns must agree — the only check that catches a harness
measuring itself rather than the engines.
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
    # 32 revisions per session costs more than the suite itself, and the bench
    # database exists for nothing else.
    return _DSN


@pytest.fixture
def conn(bench_db):
    """One connection per test, used for submit *and* for reading results back.

    Must be the same connection for both: `sync` runs every unit in the caller's
    transaction and never commits, so its `job_run` rows are visible only here.
    The DBOS units commit on their own connections, so they are visible from
    anywhere — reading on this one works for both modes.
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

    On the way in rather than out, because a killed run leaves rows behind and
    teardown never gets to argue about it.
    """
    with psycopg2.connect(_DSN) as scrub:
        scrub.autocommit = True
        with scrub.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.job_run WHERE code_version LIKE 'bench-%'"
            )
            cur.execute(
                "DELETE FROM jobs_runtime.job_unit WHERE run_id IN ("
                " SELECT run_id FROM jobs_runtime.orchestration_run"
                "  WHERE code_version LIKE 'bench-%')"
            )
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run"
                " WHERE code_version LIKE 'bench-%'"
            )
    yield


@pytest.fixture(scope="session")
def dbos_runtime(bench_db):
    """Launch DBOS once, with the environment already set.

    ⚠ Session-scoped and explicit rather than left to the first `submit_run`,
    for two measured reasons:

    1. **DBOS creates its own system schema during `launch()`.** On a fresh
       database, anything that touches `dbos.workflow_status` before that has
       happened fails with `UndefinedTable` — which is how the first run of this
       suite failed on all nine DBOS workloads while every `sync` one passed.

    2. **`launch()` is idempotent and reads the queue's `worker_concurrency`
       exactly once**, at first launch, for the life of the process. A
       `monkeypatch.setenv` in a function-scoped fixture is not reliably in place
       first, and a queue that came up at the default 2 while the report claims
       `slots: 4` would silently make the whole cross-track comparison a lie.
       `os.environ` directly, before launch, is the only ordering that holds.
    """
    os.environ["DATABASE_URL_DIRECT"] = _DSN
    os.environ["REIMAGINA_UNIT_CONCURRENCY"] = str(SLOTS)
    os.environ.setdefault("REIMAGINA_EXECUTOR_ID", f"bench-{os.getpid()}")

    from reimagina.orchestration import dbos_app

    dbos_app.launch()
    return dbos_app


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda impl: impl.mode)
def impl(request, bench_db, monkeypatch) -> Implementation:
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    monkeypatch.setenv("REIMAGINA_UNIT_CONCURRENCY", str(SLOTS))
    if request.param.mode == "dbos":
        request.getfixturevalue("dbos_runtime")
    return request.param


# ── Installing a workload ───────────────────────────────────────────────────


@pytest.fixture
def install(monkeypatch):
    """Bind a workload's synthetic computes and build its orchestrator."""

    def _install(workload, impl: Implementation):
        for slug, compute in workload.computes.items():
            monkeypatch.setitem(runners._DIRECT_COMPUTE, slug, compute)

        if impl.mode == "sync":
            from reimagina.orchestration.synchronous import SynchronousOrchestrator

            return SynchronousOrchestrator(registry=workload.registry)

        from reimagina.orchestration import dbos_app
        from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator

        # ⚠ The one patch the service track does not need. `s_compute` resolves
        # its runner with a bare `default_runners()` — no registry argument — so
        # it can only ever see SHIPPED_INDICATORS and would raise LookupError on
        # every synthetic slug. The service track's task body closes over the
        # registry it was built with and needs nothing here.
        #
        # Threading the registry into the step instead would mean putting it in
        # the workflow input, which is a production change this suite does not
        # need. Recorded as a wart in the report rather than worked around
        # silently.
        real = runners.default_runners
        monkeypatch.setattr(
            dbos_app, "default_runners", lambda: real(workload.registry)
        )
        return DBOSOrchestrator(registry=workload.registry)

    return _install


# ── Running one repetition ──────────────────────────────────────────────────


def run_once(orchestrator, conn, workload, impl: Implementation) -> dict:
    """Submit, wait for terminal, and read the run back. One repetition.

    Timestamps come from the Postgres clock, not Python's, so that
    `dispatch_lead_ms` and the parent/child ordering check are exact against the
    `job_run` rows rather than approximately right.
    """
    # A fresh code_version per submit. This track has no idempotency window to
    # defeat — unlike the service track, where an identical submit inside 60s
    # folds onto the first run — but it keeps teardown filterable and the two
    # harnesses symmetric.
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
        "unit_statuses": {
            u.indicator: u.status for u in (status.units if status else ())
        },
        "records": records,
        "t_submit": t_submit,
        "t_terminal": t_terminal,
    }


def _await_terminal(orchestrator, conn, run_id):
    deadline = time.monotonic() + _SETTLE_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        # ⚠ Roll back between polls, or the transaction opened by the first
        # `get_run` holds a snapshot from before the units committed and the
        # reads afterwards never see them.
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
        "engine_sdk": _version("dbos"),
        "git_sha": _git_sha(),
        # Not configurable here: `s_compute` is declared retries_allowed=False,
        # so a failing unit fails once. The service track retries twice with
        # backoff, which is why speedup is omitted for `partial_failure`.
        "retries_configured": 0,
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
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None
