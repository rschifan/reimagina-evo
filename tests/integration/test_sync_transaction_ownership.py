"""`sync` runs inside the caller's transaction, and the caller owns the commit.

⚠ This is asserted rather than left to a docstring because the failure is
**silent and expensive**. `SynchronousOrchestrator` executes every unit on the
connection it is handed and commits nothing, so a caller that closes or rolls
back discards the whole run — indicator results and `job_run` provenance alike —
while `submit_run` still returns a `RunHandle` reporting `succeeded`.

`scripts/bench_real_data.py` did exactly this. It compared `sync` against the
engine and reported timings for three repetitions whose output never reached
the database, and — worse for a benchmark — the engine-backed side paid a real
commit per unit while `sync` paid none, so the two columns were not measuring
the same thing.

The complementary half (a commit persists the work) is the ordinary path every
other integration test in this directory already exercises. What has no other
coverage is this: that a rollback takes the run with it.
"""

from __future__ import annotations

from reimagina.data import feature as feat
from reimagina.indicators.catalog import seed_catalog
from reimagina.orchestration.synchronous import SynchronousOrchestrator

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

_BLOCK_WKT = (
    "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
    "-99.140 19.433,-99.140 19.430))"
)


def _seed_block(conn) -> str:
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="txn_owner_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    return _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=_start_run(conn, source_id),
        dataset_version="v1",
        source_record_ref="txn-owner-blk",
        geometry_wkt=_BLOCK_WKT,
    )


def _counts(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.indicator_result "
            "WHERE job_run_id IN (SELECT id FROM jobs_runtime.job_run "
            "WHERE code_version = 'txn-ownership')"
        )
        results = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM jobs_runtime.job_run WHERE code_version = 'txn-ownership'"
        )
        return results, cur.fetchone()[0]


def test_a_sync_run_is_discarded_when_the_caller_rolls_back(conn):
    """The run reports success; the rollback still takes every row with it."""
    block_id = _seed_block(conn)

    handle = SynchronousOrchestrator().submit_run(
        conn,
        changed_attributes={"geometry"},
        edited_features={block_id},
        code_version="txn-ownership",
    )
    assert handle.status is not None and handle.status.status == "succeeded", handle

    results, job_runs = _counts(conn)
    assert results > 0 and job_runs > 0, (
        "the run should have written rows inside the caller's transaction, "
        f"got {results} results / {job_runs} job_runs"
    )

    # The caller's decision — and on this path it is the only one that matters.
    conn.rollback()

    results_after, job_runs_after = _counts(conn)
    assert (results_after, job_runs_after) == (0, 0), (
        "sync commits nothing of its own, so a caller rollback must discard the "
        f"whole run; {results_after} results and {job_runs_after} job_runs "
        "survived, which would mean the implementation started committing and "
        "the port's transaction contract needs rewriting"
    )
