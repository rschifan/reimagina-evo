"""Deep feature coverage for the Procrastinate ops-plane on real Postgres.

These exercise the *queue* behaviours the async orchestrator relies on — the
embedded-drain default, retries, terminal-failure handling, queueing-lock dedup,
worker concurrency, and durability across worker runs — against a real
``PsycopgConnector`` (not the in-memory connector), so retries, the
``queueing_lock`` unique index, and job persistence are the genuine article.

The worker tasks open their own connections (via ``DATABASE_URL_DIRECT``), so
these tests seed on a dedicated committed connection and clean up surgically —
they cannot use the rollback-per-test ``conn`` fixture. They *do* depend on the
session ``migrated_db`` fixture so the schema is present when run in isolation.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
from procrastinate import PsycopgConnector
from procrastinate.exceptions import AlreadyEnqueued

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    seed_catalog,
)
from reimagina.orchestration.embedded import _TERMINAL, _drain_once, run_embedded
from reimagina.orchestration.procrastinate_orchestrator import ProcrastinateOrchestrator
from reimagina.orchestration.queue_app import _SEARCH_PATH, app

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/reimagina_test")
_BLOCK_WKT = (
    "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
    "-99.140 19.433,-99.140 19.430))"
)
_SHIPPED_SLUGS = {
    URBAN_FORM_BLOCK_COMPACTNESS.slug,
    URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
}


def _committed_conn():
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    return conn


def _bound():
    """Bind the module ``app`` to a real Postgres connector for submit + drain."""
    return app.replace_connector(
        PsycopgConnector(conninfo=_DSN, kwargs={"options": _SEARCH_PATH})
    )


def _unit_statuses(conn, run_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indicator_slug, status FROM jobs_runtime.job_unit WHERE run_id = %s",
            (run_id,),
        )
        return dict(cur.fetchall())


def _drain_run(conn, run_id: str, *, concurrency: int = 1, timeout_s: float = 30.0):
    """Drain a fresh in-process worker until ``run_id`` is terminal.

    Mirrors ``run_embedded``'s loop but for a run that was already submitted —
    used by the durability test to submit and drain in separate steps.
    """
    orch = ProcrastinateOrchestrator()
    start = time.monotonic()
    with _bound():
        while True:
            _drain_once(concurrency)
            conn.rollback()
            status = orch.get_run(conn, run_id)
            if status is not None and status.status in _TERMINAL:
                return status
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(f"run {run_id} not terminal within {timeout_s}s")
            time.sleep(0.1)


@pytest.fixture
def seeded(migrated_db, monkeypatch):
    """A committed connection with the catalog + one block seeded.

    Yields a namespace with ``conn``, ``block_id`` and a ``track(run_id)`` hook
    so created runs get cleaned up. Teardown also clears any leftover queued
    (``todo``) Procrastinate jobs so a wedged test can't bleed into the next.
    """
    try:
        probe = psycopg2.connect(_DSN)
        probe.close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    conn = _committed_conn()
    run_ids: list[str] = []
    source_id = None
    try:
        seed_catalog(conn)
        source_id = _register_source(
            conn,
            name=f"proc_{uuid4().hex[:8]}",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        ingestion_run = _start_run(conn, source_id)
        block_type = feat.get_type_id(conn, "MEX", "block")
        block_id = _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=ingestion_run,
            dataset_version="v1",
            source_record_ref="proc-blk-1",
            geometry_wkt=_BLOCK_WKT,
        )
        yield SimpleNamespace(conn=conn, block_id=block_id, track=run_ids.append)
    finally:
        with conn.cursor() as cur:
            # Deleting a procrastinate job fires an internal trigger that
            # references unqualified procrastinate tables — needs the schema on
            # the search_path (this connection's default is public).
            cur.execute("SET search_path TO procrastinate, public")
            cur.execute(
                "DELETE FROM procrastinate.procrastinate_jobs WHERE status = 'todo'"
            )
            cur.execute("SET search_path TO public")
            for run_id in run_ids:
                cur.execute(
                    "DELETE FROM jobs_runtime.job_unit WHERE run_id = %s", (run_id,)
                )
                cur.execute(
                    "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s",
                    (run_id,),
                )
            if source_id is not None:
                cur.execute(
                    "DELETE FROM curated_geo.indicator_result WHERE feature_id IN "
                    "(SELECT id FROM curated_geo.feature WHERE source_id = %s)",
                    (source_id,),
                )
                cur.execute(
                    "DELETE FROM curated_geo.feature WHERE source_id = %s", (source_id,)
                )
                cur.execute(
                    "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
                    (source_id,),
                )
                cur.execute(
                    "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
                    (source_id,),
                )
            cur.execute("DELETE FROM jobs_runtime.job_run WHERE code_version = 'test'")
        conn.close()


def _submit(seeded, **overrides):
    kw = dict(
        changed_attributes={"geometry"},
        edited_features={seeded.block_id},
        code_version="test",
    )
    kw.update(overrides)
    return kw


# ── 1. Embedded-drain default: verifies Phase 1 end to end ──────────────────


def test_embedded_run_finalizes_succeeded(seeded):
    status = run_embedded(seeded.conn, **_submit(seeded))
    seeded.track(status.run_id)

    assert status.status == "succeeded"
    assert _unit_statuses(seeded.conn, status.run_id) == {
        s: "succeeded" for s in _SHIPPED_SLUGS
    }
    # The compactness result actually landed for the block.
    from reimagina.storage import backend_for_connection

    result = (
        backend_for_connection(seeded.conn)
        .indicator_result_repo()
        .get_current(
            feature_id=seeded.block_id,
            indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug,
        )
    )
    assert result is not None


def test_embedded_empty_dirty_set_is_noop(seeded):
    # An attribute no indicator reads → empty dirty-set → noop, nothing drained.
    status = run_embedded(
        seeded.conn,
        changed_attributes={"nonexistent_attr"},
        edited_features={seeded.block_id},
        code_version="test",
    )
    assert status.status == "noop"
    assert status.units == ()


# ── 2. Retry + backoff ──────────────────────────────────────────────────────
#
# compute_unit deliberately treats a *runner* exception as terminal (recorded
# failed, no retry — see test 3); only transient infra (a failed connection)
# propagates to Procrastinate's RetryStrategy. So the retry mechanism itself is
# exercised in isolation here with a dedicated task, proving the RetryStrategy
# the ops-plane relies on recovers a task that fails then succeeds.

from procrastinate import RetryStrategy  # noqa: E402

_retry_calls = {"n": 0}


@app.task(
    name="_test_retry_task",
    queue="compute",
    retry=RetryStrategy(max_attempts=3, exponential_wait=1),
)
def _retry_task(*, fail_until: int) -> None:
    _retry_calls["n"] += 1
    if _retry_calls["n"] < fail_until:
        raise RuntimeError("transient boom")


def _override_runner(monkeypatch, slug: str, runner):
    """Replace one slug's runner in the worker task's registry."""
    from reimagina.orchestration import runners as runners_mod

    def _patched(*args, **kwargs):
        base = runners_mod.default_runners(*args, **kwargs)
        base[slug] = runner
        return base

    monkeypatch.setattr("reimagina.orchestration.tasks.default_runners", _patched)


def test_retry_strategy_recovers_after_transient_failure(seeded):
    _retry_calls["n"] = 0
    with _bound():
        with app.open():
            _retry_task.defer(fail_until=2)
        start = time.monotonic()
        status = None
        while True:
            _drain_once(1)
            with seeded.conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = %s ORDER BY id DESC LIMIT 1",
                    ("_test_retry_task",),
                )
                row = cur.fetchone()
            status = row[0] if row else None
            if status in ("succeeded", "failed"):
                break
            if time.monotonic() - start > 30:
                raise TimeoutError("retry task did not finish")
            time.sleep(0.1)

    # Failed once, RetryStrategy re-ran it (after the backoff the drain loop had
    # to wait out), and it succeeded on the 2nd attempt.
    assert _retry_calls["n"] == 2
    assert status == "succeeded"


# ── 3. Terminal failure → partial run, other units still succeed ────────────


def test_terminal_failure_yields_partial_run(seeded, monkeypatch):
    def always_fail(conn, *, features, scenario_id, code_version, aoi_id):
        raise RuntimeError("deterministic boom")

    _override_runner(monkeypatch, URBAN_FORM_BLOCK_BUILDING_DENSITY.slug, always_fail)

    status = run_embedded(seeded.conn, **_submit(seeded))
    seeded.track(status.run_id)

    units = _unit_statuses(seeded.conn, status.run_id)
    assert units[URBAN_FORM_BLOCK_BUILDING_DENSITY.slug] == "failed"
    assert units[URBAN_FORM_BLOCK_COMPACTNESS.slug] == "succeeded"
    assert status.status == "partial"
    # A durable failed job_run was recorded on its own connection.
    with seeded.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM jobs_runtime.job_run "
            "WHERE code_version = 'test' AND status = 'failed'"
        )
        assert cur.fetchone()[0] >= 1


# ── 4. Queueing-lock dedup ──────────────────────────────────────────────────


def test_queueing_lock_dedups_duplicate_defer(seeded):
    from reimagina.orchestration.tasks import compute_unit

    lock = f"unit:{uuid4()}:x"
    defer_kwargs = dict(
        run_id="no-such-run",
        indicator_slug="x",
        feature_scope=[],
        scenario_id=None,
        aoi_id=None,
        code_version="test",
    )
    with _bound():
        with app.open():
            compute_unit.configure(queueing_lock=lock).defer(**defer_kwargs)
            with pytest.raises(AlreadyEnqueued):
                compute_unit.configure(queueing_lock=lock).defer(**defer_kwargs)

    with seeded.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM procrastinate.procrastinate_jobs "
            "WHERE queueing_lock = %s AND status = 'todo'",
            (lock,),
        )
        assert cur.fetchone()[0] == 1
    # leftover 'todo' job is cleared by the fixture teardown.


# ── 5. Worker concurrency ───────────────────────────────────────────────────


def test_concurrency_drains_all_units(seeded):
    status = run_embedded(seeded.conn, concurrency=4, **_submit(seeded))
    seeded.track(status.run_id)

    assert status.status == "succeeded"
    assert _unit_statuses(seeded.conn, status.run_id) == {
        s: "succeeded" for s in _SHIPPED_SLUGS
    }


# ── 6. Durability: jobs persist in Postgres until a worker drains them ──────


def test_jobs_persist_until_drained(seeded):
    with _bound():
        handle = ProcrastinateOrchestrator().submit_run(seeded.conn, **_submit(seeded))
    seeded.track(handle.run_id)
    assert handle.status is None  # async → not finished at submit

    # The advance_run job is durably queued in Postgres (no worker has run yet).
    with seeded.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM procrastinate.procrastinate_jobs WHERE status = 'todo'"
        )
        assert cur.fetchone()[0] >= 1
    # The run envelope is 'running', nothing computed yet.
    with seeded.conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM jobs_runtime.orchestration_run WHERE id = %s",
            (handle.run_id,),
        )
        assert cur.fetchone()[0] == "running"

    # A fresh worker (simulating a restart) drains the persisted jobs to done.
    status = _drain_run(seeded.conn, handle.run_id)
    assert status.status == "succeeded"


# ── 7. LISTEN/NOTIFY wakes an idle worker (single-loop, no OS threads) ──────

import asyncio  # noqa: E402

_notify_event: "asyncio.Event | None" = None


@app.task(name="_test_notify_task", queue="compute")
async def _notify_task() -> None:
    if _notify_event is not None:
        _notify_event.set()


def test_listen_notify_wakes_idle_worker(seeded):
    async def _run() -> float:
        global _notify_event
        _notify_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        async with app.open_async():
            worker = asyncio.create_task(
                app.run_worker_async(
                    queues=["compute"],
                    wait=True,
                    listen_notify=True,
                    fetch_job_polling_interval=30.0,  # long: NOTIFY must win the race
                    install_signal_handlers=False,
                    concurrency=1,
                )
            )
            await asyncio.sleep(0.5)  # let the worker settle into LISTEN
            t0 = loop.time()
            await _notify_task.defer_async()
            await asyncio.wait_for(_notify_event.wait(), timeout=10.0)
            elapsed = loop.time() - t0
            worker.cancel()  # graceful cancel of the idle worker
            try:
                await worker
            except asyncio.CancelledError:
                pass
            return elapsed

    with _bound():
        elapsed = asyncio.run(_run())
    # Woken by NOTIFY, not the 30s poll fallback.
    assert elapsed < 5.0


# ── 8. A persistent wait=True worker drains a real run, then stops cleanly ──


def test_background_worker_processes_run_then_stops(seeded):
    with _bound():
        handle = ProcrastinateOrchestrator().submit_run(seeded.conn, **_submit(seeded))
        seeded.track(handle.run_id)

        async def _run():
            orch = ProcrastinateOrchestrator()
            loop = asyncio.get_event_loop()
            async with app.open_async():
                worker = asyncio.create_task(
                    app.run_worker_async(
                        wait=True,
                        listen_notify=True,
                        fetch_job_polling_interval=1.0,
                        install_signal_handlers=False,
                        concurrency=2,
                    )
                )
                start = loop.time()
                status = None
                while True:
                    await asyncio.sleep(0.2)
                    seeded.conn.rollback()
                    status = orch.get_run(seeded.conn, handle.run_id)
                    if status is not None and status.status in _TERMINAL:
                        break
                    if loop.time() - start > 30:
                        raise TimeoutError("background run did not finish")
                worker.cancel()  # graceful shutdown after the work is done
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                return status

        status = asyncio.run(_run())

    assert status.status == "succeeded"
    assert _unit_statuses(seeded.conn, handle.run_id) == {
        s: "succeeded" for s in _SHIPPED_SLUGS
    }
