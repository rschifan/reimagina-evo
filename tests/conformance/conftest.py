"""Fixtures for the port conformance suite.

``IMPLEMENTATIONS`` is the branch-specific half of this suite: which
orchestrators exist here, and what each one is capable of. The assertions in
``test_port_contract.py`` are meant to be identical across branches; this table
is what an ops-plane branch edits.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    SHIPPED_INDICATORS,
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

_LOG = logging.getLogger(__name__)

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

COMPOSITE_SLUGS = frozenset(
    s.slug for s in SHIPPED_INDICATORS if s.computation_kind == "composite"
)
"""Downstream units, taken from the catalog rather than assumed.

⚠ Not "everything that is not the two slugs above". The catalog ships three
*direct* indicators, and the baseline `geometry` edit dirties only two of them —
so exclusion also catches `planned_floors`, which is legitimately `skipped` for
a quite different reason than a composite whose input failed."""


@dataclass(frozen=True)
class Implementation:
    """One orchestrator this branch ships, and what it can be asked to prove."""

    mode: str

    blocking: bool
    """``submit_run`` does not return until the run is terminal.

    False means a run can be observed in flight — the only way to test a cancel
    that actually cancels something."""

    settles: bool
    """The run reaches a terminal state without a second process."""

    skipped_unit_status: str
    """What a unit that never ran because its input failed reports.

    ``skipped`` where we own unit state — it says *why*. The engine calls it
    ``cancelled``, which does not (spike B3)."""

    failed_run_status: str
    """What a run with one failed and one succeeded unit reports.

    ⚠ **``partial`` on both implementations, and that is a correction.** This
    field previously said ``failed`` for ``hatchet``, on the reasoning that
    Hatchet's status enum is QUEUED | RUNNING | COMPLETED | CANCELLED | FAILED
    (measured, spike B7) and has no word for a run that half-worked.

    The enum still has no such word. But the run's *output* does: the workflow's
    ``on_failure_task`` reconstructs a per-unit verdict from ``task_run_errors``,
    ``was_skipped`` and ``task_output``, and ``get_run`` reads that in preference
    to the enum (spike B8). So the vocabulary survives the engine owning run
    state; what does not survive is *querying across runs*, which is a property
    of the storage, not of the status model.

    The headline result of the comparison is therefore not this field. It is
    that one branch can ``GROUP BY`` its run history and the other must ask the
    engine one run at a time."""


IMPLEMENTATIONS = [
    Implementation(
        mode="sync",
        blocking=True,
        settles=True,
        skipped_unit_status="skipped",
        failed_run_status="partial",
    ),
    # The branch's own ops-plane. Needs a running engine AND a worker — unlike
    # the library track, nothing executes without one. `settles` is True only
    # because the `hatchet_worker` fixture starts one in-process; there is no
    # configuration in which this implementation settles on its own.
    Implementation(
        mode="hatchet",
        blocking=False,
        settles=True,
        # ⚠ `cancelled`, not `skipped`. The engine cancels a dependent whose
        # input failed (B3) and the verdict reports what happened rather than
        # why. The library track's `skipped` carries the reason; this does not,
        # and no amount of reconstruction recovers it — the engine never knew.
        skipped_unit_status="cancelled",
        failed_run_status="partial",
    ),
]


@pytest.fixture(scope="session")
def hatchet_worker():
    """A worker, in this process, for the whole session.

    ⚠ **This fixture is itself a result.** The `sync` implementation needs
    nothing; `hatchet` needs a reachable engine *and* a second party willing to
    execute, because a submitted run is not merely un-executed without one — it
    is un-*observable*: `runs.get()` returns 404 on an id the engine just
    minted. The library track's suite starts no equivalent, and that asymmetry
    is the point rather than a fixture detail.

    In-process, in a daemon thread, rather than a subprocess: the `poison`
    fixture monkeypatches ``runners._DIRECT_COMPUTE``, and ``default_runners``
    reads that dict at call time inside the task body, so a same-process worker
    sees the patch and a subprocess would not.

    Session-scoped because worker startup costs seconds of engine registration,
    and because the DAG declaration is now deterministic across builds — two
    workers in one session would otherwise be a version-churn hazard.
    """
    import os
    import threading

    if not os.environ.get("HATCHET_CLIENT_TOKEN"):
        pytest.skip("HATCHET_CLIENT_TOKEN unset — no engine to register with")

    os.environ.setdefault("DATABASE_URL_DIRECT", _DSN)
    from apps.worker.app import run_hatchet_worker

    try:
        worker = run_hatchet_worker(name="conformance", slots=4, blocking=False)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not build a Hatchet worker: {exc}")

    thread = threading.Thread(target=worker.start, daemon=True)
    thread.start()
    _await_registration(worker)
    yield worker

    _stop_worker(worker)


def _stop_worker(worker, timeout: float = 20.0) -> None:
    """Stop the worker through the SDK's own API, so the session can exit.

    ⚠ One SDK fact makes the obvious call wrong, and it is the reason this
    suite once concluded that a clean exit leaks a registration exactly like a
    `SIGKILL` does: **`exit_gracefully` is `async def`**. Called from sync code
    it returns a coroutine that is never awaited — Python only warns — so it
    does nothing whatsoever. Awaited properly it does deregister: its first act
    is `_pause_task_assignment()`, "tell the engine that the worker is paused".
    The leak was never engine behaviour; it was a coroutine on the floor.

    So it is driven on the worker's own loop. The listener is a spawned child
    process and not a daemon, so Python joins it at interpreter exit and a
    listener that never stops hangs the session *after the last test passed* —
    no output, no failing test. `exit_gracefully` stops it; the check below is
    a bounded safety net, and it reads the **public** `action_listener_process`
    property rather than the private attribute behind it.
    """
    loop = getattr(worker, "loop", None)
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(worker.exit_gracefully(), loop).result(
                timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 — teardown must not mask a failure
            _LOG.warning("graceful worker stop failed: %s", exc)

    for attr in ("action_listener_process", "durable_action_listener_process"):
        proc = getattr(worker, attr, None)
        if proc is not None and proc.is_alive():
            _LOG.warning("%s survived graceful stop; terminating", attr)
            proc.terminate()
            proc.join(timeout=5.0)


# ⚠ No `_reap_stale_workers`. It paused every `conformance` worker the engine
# still listed, on setup, because stale registrations were believed to stay
# ACTIVE and keep being handed work — the suite's wall clock drifting 62s → 322s
# was the evidence. Measured again at v0.86.24: fourteen accumulated
# registrations, every one of them INACTIVE, the oldest eight hours stale. The
# engine expires a worker on heartbeat timeout. The row persisting is history,
# not eligibility, and the two were being conflated.
#
# What actually leaked was a coroutine: `exit_gracefully` was called from sync
# code and never awaited, so nothing ever told the engine the worker had gone.
# See `_stop_worker`.


def _await_registration(worker, timeout: float = 30.0) -> None:
    """Block until the engine will answer for a run, or skip.

    Polling the *symptom* rather than a worker-ready flag: what the suite needs
    is not "the worker started" but "a submitted run is retrievable", and only
    the engine can answer that.
    """
    import time

    from reimagina.orchestration import hatchet_app

    deadline = time.monotonic() + timeout
    last: object = "no response"
    while time.monotonic() < deadline:
        try:
            listed = hatchet_app.get_hatchet().workflows.list()
            names = {
                getattr(w, "name", None) for w in (getattr(listed, "rows", None) or [])
            }
            if "reimagina-run" in names:
                return
            last = f"registered workflows: {sorted(n for n in names if n)}"
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    pytest.skip(f"worker did not register within {timeout}s: {last}")


@pytest.fixture(params=IMPLEMENTATIONS, ids=lambda impl: impl.mode)
def impl(request, migrated_db, monkeypatch) -> Implementation:
    """The implementation under test, with its environment satisfied."""
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    if request.param.mode == "hatchet":
        request.getfixturevalue("hatchet_worker")
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
    """Clean the domain rows. There is no run state of ours left to clean.

    ⚠ This function used to delete queued jobs, ``job_unit`` and
    ``orchestration_run``. All three are gone (migration r033) — run state
    belongs to the engine now, and runs there expire on the engine's own
    retention schedule rather than being cleaned up by a test. ``run_ids`` is
    still tracked because the tests read it; nothing here deletes it.
    """
    with conn.cursor() as cur:
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
        # LIKE, not `=`: the submit fixture tags each run with its own
        # version suffix so the dedup window does not collapse them.
        cur.execute(
            "DELETE FROM jobs_runtime.job_run WHERE code_version LIKE 'conformance%'"
        )


@pytest.fixture
def submit(orchestrator, seeded):
    """Submit a real dirty-set run and track it for cleanup.

    ⚠ ``code_version`` is unique per call, and that is not cosmetic. Every test
    here submits the same edit to the same feature, seconds apart — which is
    *exactly* what run-level deduplication exists to collapse. Once the Hatchet
    workflow declared an idempotency window, five of these tests began failing
    with ``IdempotencyCollisionError``: the second submit was correctly refused
    and handed back the first run's id, so tests expecting an independent run
    got a finished one.

    That is the feature working. A suite that wants N independent runs must
    present N distinct pieces of work, so each submission gets its own version
    tag. The alternative — shortening the window for tests — would leave the
    suite exercising a configuration nothing deploys.

    Recorded because it is a real difference: the library track's suite needs no
    such care, since nothing there deduplicates unless we write it.
    """

    def _submit(**overrides):
        kwargs = dict(
            changed_attributes={"geometry"},
            edited_features={seeded.block_id},
            code_version=f"conformance-{uuid4().hex[:8]}",
        )
        kwargs.update(overrides)
        handle = orchestrator.submit_run(seeded.conn, **kwargs)
        seeded.track(handle.run_id)
        return handle

    return _submit
