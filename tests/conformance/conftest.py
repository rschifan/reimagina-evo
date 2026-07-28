"""Fixtures for the port conformance suite.

``IMPLEMENTATIONS`` is the branch-specific half of this suite: which
orchestrators exist here, and what each one is capable of. The assertions in
``test_port_contract.py`` are meant to be identical across branches; this table
is what an ops-plane branch edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
)
from reimagina.orchestration.factory import get_orchestrator

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

# The unit poisoned to produce a partial run. It must be one of two independent
# direct indicators, so the other still succeeds — a run where everything failed
# would not distinguish `partial` from `failed`.
POISONED_SLUG = URBAN_FORM_BLOCK_BUILDING_DENSITY.slug
HEALTHY_SLUG = URBAN_FORM_BLOCK_COMPACTNESS.slug


@dataclass(frozen=True)
class Implementation:
    """One orchestrator this branch ships, and what it can be asked to prove."""

    mode: str

    blocking: bool
    """``submit_run`` does not return until the run is terminal.

    False means a run can be observed in flight — the only way to test a cancel
    that actually cancels something."""

    failed_run_status: str
    """What a run with one failed and one succeeded unit reports.

    ``partial`` where we own run state. An engine that owns it natively may have
    no such value; that branch declares whatever it does say here, and the
    difference between the two tables is the finding, not a failure."""


IMPLEMENTATIONS = [
    Implementation(mode="sync", blocking=True, failed_run_status="partial"),
    Implementation(mode="embedded", blocking=True, failed_run_status="partial"),
    Implementation(mode="detach", blocking=False, failed_run_status="partial"),
]


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda impl: impl.mode)
def impl(request, migrated_db, monkeypatch) -> Implementation:
    """The implementation under test, with its environment satisfied."""
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    return request.param


@pytest.fixture
def orchestrator(impl):
    """Built through the factory, never by name — the seam is what is under test."""
    return get_orchestrator(impl.mode)


@pytest.fixture
def poison(monkeypatch):
    """Make one indicator's compute raise, wherever it is dispatched from.

    Patches the compute registry rather than a ``default_runners`` import, so it
    applies no matter which module resolved the runner — the in-process
    coordinator and the queue worker look it up differently.
    """
    from reimagina.orchestration import runners

    def _boom(*args, **kwargs):
        raise RuntimeError("poisoned by the conformance suite")

    monkeypatch.setitem(runners._DIRECT_COMPUTE, POISONED_SLUG, _boom)


@pytest.fixture
def seeded(migrated_db):
    """A committed connection with the catalog and one block seeded.

    Committed, not the rollback ``conn`` fixture: a queue worker opens its own
    connection and would not see uncommitted rows. Cleanup is surgical, and
    includes any queued jobs a non-blocking submit left behind.

    Takes ``migrated_db`` explicitly. Bypassing the rollback fixture also
    bypasses the migration it runs, and without this the schema is present
    only because some other fixture happened to ask for it first — the exact
    ordering dependency that broke the engine tests when they moved.
    """
    try:
        psycopg2.connect(_DSN).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    run_ids: list[str] = []
    source_id = None
    try:
        seed_catalog_committed(conn)
        source_id = _register_source(
            conn,
            name=f"conf_{uuid4().hex[:8]}",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        block_id = _insert_feature(
            conn,
            type_id=feat.get_type_id(conn, "MEX", "block"),
            source_id=source_id,
            ingestion_run_id=_start_run(conn, source_id),
            dataset_version="v1",
            source_record_ref="conf-blk-1",
            geometry_wkt=_BLOCK_WKT,
        )
        yield SimpleNamespace(conn=conn, block_id=block_id, track=run_ids.append)
    finally:
        _teardown(conn, run_ids, source_id)
        conn.close()


def _teardown(conn, run_ids, source_id) -> None:
    with conn.cursor() as cur:
        # A non-blocking submit leaves an undrained job; deleting one fires an
        # internal trigger that references unqualified procrastinate tables.
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
                "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s", (run_id,)
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
        cur.execute(
            "DELETE FROM jobs_runtime.job_run WHERE code_version = 'conformance'"
        )


@pytest.fixture
def submit(orchestrator, seeded):
    """Submit a real dirty-set run and track it for cleanup."""

    def _submit(**overrides):
        kwargs = dict(
            changed_attributes={"geometry"},
            edited_features={seeded.block_id},
            code_version="conformance",
        )
        kwargs.update(overrides)
        handle = orchestrator.submit_run(seeded.conn, **kwargs)
        seeded.track(handle.run_id)
        return handle

    return _submit
