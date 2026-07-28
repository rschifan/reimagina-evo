"""End-to-end async orchestration: submit → Procrastinate worker → results.

The worker's tasks open their OWN connections, so this test can't use the
rollback-per-test ``conn`` fixture — it seeds on a dedicated committed
connection and cleans up surgically in a finally. The queue runs on
``InMemoryConnector`` (no broker); ``compute_unit`` still hits real Postgres for
the domain writes. Requires the Postgres service (skips otherwise), and takes
``migrated_db`` explicitly — bypassing the rollback ``conn`` fixture also
bypasses the migration it runs.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import psycopg2
import pytest
from procrastinate.testing import InMemoryConnector

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_COMPOSITE,
)
from reimagina.orchestration.impact import compute_dirty_set

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    seed_catalog_committed,
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


def _drain_worker():
    """Run the Procrastinate worker until the in-memory queue is empty."""
    from reimagina.orchestration.queue_app import app

    async def _run():
        async with app.open_async():
            await app.run_worker_async(
                wait=False, listen_notify=False, install_signal_handlers=False
            )

    asyncio.run(_run())


def test_async_run_computes_shipped_indicators_end_to_end(migrated_db, monkeypatch):
    try:
        probe = psycopg2.connect(_DSN)
        probe.close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )
    from reimagina.orchestration.queue_app import app

    source_id = None
    run_id = None
    conn = _committed_conn()
    try:
        # Seed catalog + one block, committed so the worker's own connection sees it.
        seed_catalog_committed(conn)
        source_id = _register_source(
            conn,
            name=f"async_{uuid4().hex[:8]}",
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
            source_record_ref="async-blk-1",
            geometry_wkt=_BLOCK_WKT,
        )

        with app.replace_connector(InMemoryConnector()):
            handle = ProcrastinateOrchestrator().submit_run(
                conn,
                changed_attributes={"geometry"},
                edited_features={block_id},
                code_version="test",
            )
            run_id = handle.run_id
            assert handle.status is None  # async → caller polls
            _drain_worker()

        # Run finalized succeeded; every dirty unit succeeded — including the
        # composite, which only becomes runnable in a second wave.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM jobs_runtime.orchestration_run WHERE id = %s",
                (run_id,),
            )
            assert cur.fetchone()[0] == "succeeded"
            cur.execute(
                "SELECT indicator_slug, status FROM jobs_runtime.job_unit "
                "WHERE run_id = %s",
                (run_id,),
            )
            statuses = dict(cur.fetchall())
        expected = {
            u.indicator for u in compute_dirty_set({"geometry"}, (), SHIPPED_INDICATORS)
        }
        assert URBAN_FORM_BLOCK_COMPOSITE.slug in expected  # fan-out is exercised
        assert statuses == dict.fromkeys(expected, "succeeded")

        # Compactness result landed for the block.
        from reimagina.storage import backend_for_connection

        result = (
            backend_for_connection(conn)
            .indicator_result_repo()
            .get_current(
                feature_id=block_id,
                indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug,
            )
        )
        assert result is not None
    finally:
        with conn.cursor() as cur:
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
