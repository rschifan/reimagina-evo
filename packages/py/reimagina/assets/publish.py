"""Publishing recorded versions for source and external assets.

A source asset's version is *recorded by whatever changed it* — this module is
where that happens. It is the half of the algebra the database cannot derive:
``serving_geo.asset_expected_version_v`` computes every derived version from the
graph, but the leaves have to be told.

Iceberg-snapshot semantics: each publish appends a row and moves ``is_current``.
The version string is the ingestion run's own id, because that is exactly the
identity of "the event that changed this layer" — not a content hash, which
would cost a full scan of a multi-million-row table on every ingest to answer a
question the run boundary already answers.

⚠ Publishing is deliberately gated on rows actually being written. Vector loads
are ``ON CONFLICT (source_id, dataset_version, source_record_ref) DO NOTHING``,
so re-running an ingest at the same release is a genuine no-op — bumping the
version for it would stale every downstream indicator and recompute the whole
platform to arrive at identical numbers.
"""

from __future__ import annotations

from typing import Any

from reimagina.assets.keys import BASELINE


def asset_key_for_feature_type(conn: Any, type_id: str) -> str | None:
    """The source asset backing a feature type, if one is registered.

    Resolved through ``asset.feature_type_id`` rather than by rebuilding a
    ``context:type_slug`` string, so the FK is what links them and a renamed slug
    cannot silently orphan the mapping.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key FROM curated_geo.asset "
            "WHERE feature_type_id = %s AND kind IN ('source', 'external')",
            (type_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def publish_version(
    conn: Any,
    *,
    asset_key: str,
    version: str,
    partition_key: str = BASELINE,
    ingestion_run_id: str | None = None,
    dataset_version: str | None = None,
    row_count: int | None = None,
) -> str | None:
    """Record a new current version for one asset partition.

    Returns the new version, or None when the asset is not registered — an
    unregistered layer is not an error, it simply does not participate in the
    graph yet, and an ingest must not fail because of that.

    Idempotent on ``version``: re-publishing the value already current is a
    no-op, so a retried ingest does not manufacture a change.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM curated_geo.asset WHERE asset_key = %s", (asset_key,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        asset_id = row[0]

        cur.execute(
            "SELECT version FROM curated_geo.asset_version "
            "WHERE asset_id = %s AND partition_key = %s AND is_current",
            (asset_id, partition_key),
        )
        current = cur.fetchone()
        if current and current[0] == version:
            return version
        # max(sequence) over the whole partition history, not the live row's + 1.
        # Zero current rows is a legal state — asset_version_one_current is a
        # *partial* unique index — so deriving from the live row would restart at
        # 1 after a retire-without-replace and collide with retired history.
        cur.execute(
            "SELECT COALESCE(max(sequence), 0) + 1 FROM curated_geo.asset_version "
            "WHERE asset_id = %s AND partition_key = %s",
            (asset_id, partition_key),
        )
        next_sequence = cur.fetchone()[0]

        cur.execute(
            "UPDATE curated_geo.asset_version SET is_current = false, valid_to = now() "
            "WHERE asset_id = %s AND partition_key = %s AND is_current",
            (asset_id, partition_key),
        )
        cur.execute(
            """
            INSERT INTO curated_geo.asset_version
              (asset_id, partition_key, version, sequence, ingestion_run_id,
               dataset_version, row_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                partition_key,
                version,
                next_sequence,
                ingestion_run_id,
                dataset_version,
                row_count,
            ),
        )
    return version


def retire_version(
    conn: Any,
    *,
    asset_key: str,
    partition_key: str = BASELINE,
) -> str | None:
    """Withdraw an asset partition's current version without replacing it.

    Returns the version that was retired, or None when the asset is not
    registered or nothing was current — both are states, not errors.

    ``publish_version`` can only say "this layer is now X". Some things that
    happen to a layer are not a new X: it is truncated, dropped, restored to a
    machine where it does not exist, or its probe finds nothing left to signal
    on. Without this the only way to express that is to publish a sentinel
    string, which makes the asset read ``fresh`` at a version naming its own
    absence — the exact shape of lie the whole design exists to remove.

    Retiring instead is a real state everywhere downstream, with no new
    vocabulary: the partition drops out of ``asset_version``'s current set, so
    ``asset_freshness_v`` reports it ``absent`` rather than fresh, and its
    component in ``asset_component_v`` falls to ``'missing'`` — which is stable,
    so every consumer goes deterministically stale and stays there until
    something is published again.

    Zero current rows is already a legal state: ``asset_version_one_current`` is
    a *partial* unique index, and ``publish_version`` derives its next sequence
    from ``max(sequence)`` over the whole history precisely so a later publish
    lands after retired history rather than colliding with it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE curated_geo.asset_version av
               SET is_current = false, valid_to = now()
              FROM curated_geo.asset a
             WHERE a.id = av.asset_id
               AND a.asset_key = %s
               AND av.partition_key = %s
               AND av.is_current
            RETURNING av.version
            """,
            (asset_key, partition_key),
        )
        row = cur.fetchone()
        return row[0] if row else None


def backfill_versions(conn: Any) -> list[dict[str, Any]]:
    """Publish a version for every source asset that has data but no version.

    The ingest path publishes on completion (``data.catalog.finish_run``), so
    this is only for layers loaded *before* the asset graph existed. Without it
    those leaves stay unversioned, ``asset_component_v`` hashes them as the
    literal ``'missing'``, and every indicator downstream reads stale forever —
    a graph that is wired correctly and permanently wrong.

    The version is the id of the most recent succeeded ingestion run that
    touched the backing feature type, matching what a fresh load would have
    recorded. Assets already carrying a current version are left alone: a
    backfill that bumped them would stale the whole platform to recompute
    identical numbers.
    """
    published: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.asset_key, a.backing_ref, ft.id::text
            FROM curated_geo.asset a
            JOIN curated_geo.feature_type_catalog ft ON ft.id = a.feature_type_id
            WHERE a.kind IN ('source', 'external')
              AND a.version_strategy = 'commit'
              AND NOT EXISTS (
                  SELECT 1 FROM curated_geo.asset_version v
                  WHERE v.asset_id = a.id AND v.is_current
              )
            ORDER BY a.asset_key
            """
        )
        candidates = [(r[0], r[1], r[2]) for r in cur.fetchall()]

    for asset_key, backing_ref, type_id in candidates:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.ingestion_run_id::text,
                       max(f.dataset_version) AS dataset_version,
                       count(*) AS row_count
                FROM curated_geo.feature f
                JOIN curated_geo.ingestion_run r ON r.id = f.ingestion_run_id
                WHERE f.type_id = %s AND r.status = 'succeeded'
                GROUP BY f.ingestion_run_id, r.completed_at
                ORDER BY r.completed_at DESC NULLS LAST
                LIMIT 1
                """,
                (type_id,),
            )
            row = cur.fetchone()
        if row is None:
            # Registered but never loaded — correctly left absent rather than
            # given a fabricated version.
            continue
        run_id, dataset_version, row_count = row
        version = publish_version(
            conn,
            asset_key=asset_key,
            version=str(run_id),
            ingestion_run_id=run_id,
            dataset_version=dataset_version,
            row_count=row_count,
        )
        published.append(
            {
                "asset_key": asset_key,
                "backing_ref": backing_ref,
                "version": version,
                "row_count": row_count,
            }
        )
    return published
