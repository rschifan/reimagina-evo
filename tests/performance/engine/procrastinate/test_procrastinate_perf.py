"""Performance smoke for the Procrastinate ops-plane on real Postgres.

Prints a p50/p95/p99 enqueue-latency + throughput table and asserts *generous*
budgets — the numbers are informational (run with ``-s`` to see them), the
assertions only guard against a gross regression. Skips cleanly when Postgres is
unavailable. Not part of the fast unit lane.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import seed_catalog
from reimagina.orchestration.embedded import run_embedded
from reimagina.perf.procrastinate_bench import format_report, run_benchmarks

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


def _committed_conn():
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    return conn


@pytest.fixture
def perf_env(migrated_db, monkeypatch):
    try:
        psycopg2.connect(_DSN).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    return _committed_conn()


def test_queue_benchmark_reports_and_meets_budgets(perf_env, capsys):
    conn = perf_env
    try:
        report = run_benchmarks(conn, enqueue_count=100, concurrencies=(1, 4, 8))
    finally:
        conn.close()

    with capsys.disabled():
        print("\n" + format_report(report))

    # Generous budgets — informational, not brittle.
    assert report.submit_latency_ms.p95_ms < 100.0  # a single INSERT
    assert report.throughput_units_per_s[1] > 5.0  # 100 jobs shouldn't take >20s
    assert report.per_job_overhead_ms < 250.0
    # Concurrency should not collapse throughput (it may not scale linearly for
    # DB-bound no-op jobs, but it must not regress below the serial baseline).
    assert report.throughput_units_per_s[8] >= report.throughput_units_per_s[1] * 0.75


def test_end_to_end_embedded_run_latency(perf_env, capsys):
    conn = perf_env
    source_id = None
    run_id = None
    try:
        seed_catalog(conn)
        source_id = _register_source(
            conn,
            name=f"perf_{uuid4().hex[:8]}",
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
            source_record_ref="perf-blk-1",
            geometry_wkt=_BLOCK_WKT,
        )

        t0 = time.perf_counter()
        status = run_embedded(
            conn,
            changed_attributes={"geometry"},
            edited_features={block_id},
            code_version="test",
        )
        elapsed = time.perf_counter() - t0
        run_id = status.run_id

        with capsys.disabled():
            print(
                f"\nembedded run latency (submit → finalize, "
                f"{len(status.units)} units): {elapsed * 1000:.0f} ms"
            )
        assert status.status == "succeeded"
        assert elapsed < 15.0  # generous end-to-end budget
    finally:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO procrastinate, public")
            cur.execute("DELETE FROM procrastinate.procrastinate_jobs")
            cur.execute("SET search_path TO public")
            if run_id is not None:
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
