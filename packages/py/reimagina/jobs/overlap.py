"""Feature-to-feature overlap orchestration.

Wraps OverlapRepository.compute_feature_overlaps so callers can declare
(source_type, target_type) pairs once in feature_overlap_type_pair and
then trigger a whole batch by AOI. Intended for CLI use and, later, a
real async worker.
"""
from __future__ import annotations

from typing import Any

from reimagina.data.feature import get_type_id
from reimagina.storage import backend_for_connection


def declare_pair(
    conn,
    *,
    source_context: str,
    source_slug: str,
    target_context: str,
    target_slug: str,
) -> dict[str, str]:
    """Idempotently register a source_type → target_type pair for overlap compute."""
    source_type_id = get_type_id(conn, source_context, source_slug)
    target_type_id = get_type_id(conn, target_context, target_slug)
    backend = backend_for_connection(conn)

    if backend.name == "duckdb":
        conn.execute(
            """
            INSERT INTO curated_geo.feature_overlap_type_pair (source_type_id, target_type_id)
            VALUES (?, ?)
            ON CONFLICT (source_type_id, target_type_id) DO NOTHING
            """,
            [source_type_id, target_type_id],
        )
    else:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO curated_geo.feature_overlap_type_pair (source_type_id, target_type_id)
                VALUES (%s, %s)
                ON CONFLICT (source_type_id, target_type_id) DO NOTHING
                """,
                (source_type_id, target_type_id),
            )

    return {
        "source_type_id": source_type_id,
        "target_type_id": target_type_id,
        "source": f"{source_context}:{source_slug}",
        "target": f"{target_context}:{target_slug}",
    }


def list_pairs(conn) -> list[dict[str, Any]]:
    """Return declared type-pairs joined with their catalog slugs."""
    sql = """
        SELECT
            CAST(p.source_type_id AS VARCHAR) AS source_type_id,
            sc.context || ':' || sc.type_slug  AS source,
            CAST(p.target_type_id AS VARCHAR) AS target_type_id,
            tc.context || ':' || tc.type_slug  AS target
        FROM curated_geo.feature_overlap_type_pair p
        JOIN curated_geo.feature_type_catalog sc ON sc.id = p.source_type_id
        JOIN curated_geo.feature_type_catalog tc ON tc.id = p.target_type_id
        ORDER BY sc.context, sc.type_slug, tc.context, tc.type_slug
    """
    backend = backend_for_connection(conn)
    if backend.name == "duckdb":
        rows = conn.execute(sql).fetchall()
        cols = ("source_type_id", "source", "target_type_id", "target")
        return [dict(zip(cols, row)) for row in rows]

    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def compute_all_pairs(conn, *, aoi_id: str | None = None) -> dict[str, Any]:
    """Run compute_feature_overlaps for every declared pair.

    Returns per-pair row counts plus a total. Scope the run with aoi_id to
    avoid unbounded cross-joins at city scale.
    """
    pairs = list_pairs(conn)
    overlap_repo = backend_for_connection(conn).overlap_repo()
    per_pair: list[dict[str, Any]] = []
    total = 0
    for pair in pairs:
        count = overlap_repo.compute_feature_overlaps(
            pair["source_type_id"],
            pair["target_type_id"],
            aoi_id=aoi_id,
        )
        per_pair.append({"source": pair["source"], "target": pair["target"], "rows": count})
        total += count
    return {"aoi_id": aoi_id, "pairs": per_pair, "rows_total": total}
