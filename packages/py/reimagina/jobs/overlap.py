"""Feature-to-feature overlap orchestration.

Reads declared (source_type, target_type) pairs from the type-pair
repository and fans out to OverlapRepository.compute_feature_overlaps.
Kept backend-agnostic: the repositories own all SQL dialect work.
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
    """Register a source_type → target_type pair. Idempotent."""
    source_type_id = get_type_id(conn, source_context, source_slug)
    target_type_id = get_type_id(conn, target_context, target_slug)

    backend_for_connection(conn).overlap_type_pair_repo().upsert_pair(
        source_type_id, target_type_id
    )

    return {
        "source_type_id": source_type_id,
        "target_type_id": target_type_id,
        "source": f"{source_context}:{source_slug}",
        "target": f"{target_context}:{target_slug}",
    }


def list_pairs(conn) -> list[dict[str, Any]]:
    """Return declared type-pair rows joined with their catalog slugs."""
    return backend_for_connection(conn).overlap_type_pair_repo().list_pairs()


def compute_all_pairs(conn, *, aoi_id: str | None = None) -> dict[str, Any]:
    """Run compute_feature_overlaps for every declared pair.

    Scope with `aoi_id` to avoid unbounded cross joins at city scale.
    Returns per-pair row counts plus a total.
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
        per_pair.append(
            {"source": pair["source"], "target": pair["target"], "rows": count}
        )
        total += count
    return {"aoi_id": aoi_id, "pairs": per_pair, "rows_total": total}
