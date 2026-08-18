"""Recording what a compute actually produced.

Until something writes here, freshness can only ever answer ``absent``: the
graph knows what each indicator *should* be, and nothing tells it what it *is*.
This module is the other half, and it is what makes the whole design operate on
real data rather than in tests.

Three things get recorded, and each answers a question nothing else could:

* **the version** the result was computed against — so "is this out of date" is
  a comparison rather than a guess;
* **the input versions** that went into it — F8, and the reason a number can be
  explained a month later;
* **the coverage** — whether the run covered the whole grain or a subset, which
  is what lets a partition report ``partial`` instead of claiming to be done.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from reimagina.assets.keys import BASELINE, indicator_asset_key


def expected_version(
    conn: Any, asset_key: str, partition_key: str = BASELINE
) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_version FROM serving_geo.asset_expected_version_v "
            "WHERE asset_key = %s AND partition_key = %s",
            (asset_key, partition_key),
        )
        row = cur.fetchone()
        return row[0] if row else None


def input_versions(
    conn: Any, asset_key: str, partition_key: str = BASELINE
) -> dict[str, str]:
    """The recorded versions of every source asset upstream of this one.

    Only source and external assets carry recorded versions; a derived
    upstream's version is a function of these, so storing it too would be
    storing the same fact twice.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT av.asset_key || '@' || av.partition_key, av.version
            FROM serving_geo.asset_lineage_v l
            JOIN curated_geo.asset a ON a.asset_key = l.related_key
            JOIN (
                SELECT v.asset_id, v.partition_key, v.version, s.asset_key
                FROM curated_geo.asset_version v
                JOIN curated_geo.asset s ON s.id = v.asset_id
                WHERE v.is_current
            ) av ON av.asset_id = a.id
            WHERE l.direction = 'upstream' AND l.root_key = %s
              AND a.kind IN ('source', 'external')
              AND av.partition_key IN ('', %s)
            """,
            (asset_key, partition_key),
        )
        return dict(cur.fetchall())


def record(
    conn: Any,
    *,
    asset_key: str,
    version: str,
    partition_key: str = BASELINE,
    inputs: dict[str, str] | None = None,
    job_run_id: str | None = None,
    row_count: int | None = None,
    complete: bool = True,
    status: str = "succeeded",
) -> None:
    """Append one materialization record."""
    import json

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM curated_geo.asset WHERE asset_key = %s", (asset_key,)
        )
        row = cur.fetchone()
        if row is None:
            return
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization
              (asset_id, partition_key, version, input_versions, coverage_scope,
               job_run_id, row_count, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row[0],
                partition_key,
                version,
                json.dumps(inputs or {}),
                json.dumps({"complete": complete}),
                job_run_id,
                row_count,
                status,
            ),
        )


@contextmanager
def materializing(
    conn: Any,
    *,
    slug: str,
    partition_key: str = BASELINE,
    complete: bool = True,
):
    """Record what a compute produced, versioned against what it read.

    ⚠ The expected version is captured **before** the compute runs, not after.
    A compute reads its inputs at the start; if an ingest lands while it is
    running, recording the *post* version would stamp the result as current
    against inputs it never saw — a false "fresh", which is the dangerous
    direction to be wrong in. Capturing first means such a result reports stale
    and gets recomputed: wasteful, never wrong.

    Yields a mutable dict the caller fills in with row_count and job_run_id from
    whatever the compute returned. Nothing is written if the compute raises —
    a failed run leaves the previous materialization standing, so freshness
    keeps reporting the state that is actually on disk.
    """
    from reimagina.assets.freshness import graph_available

    if not graph_available(conn):
        yield {}
        return

    asset_key = indicator_asset_key(slug)
    version = expected_version(conn, asset_key, partition_key)
    inputs = input_versions(conn, asset_key, partition_key)
    outcome: dict[str, Any] = {}
    yield outcome
    if version is None:
        return
    record(
        conn,
        asset_key=asset_key,
        version=version,
        partition_key=partition_key,
        inputs=inputs,
        job_run_id=outcome.get("job_run_id"),
        row_count=outcome.get("row_count"),
        complete=complete,
    )
