"""CLI helpers for feature-to-feature overlap orchestration."""
from __future__ import annotations

from reimagina.data.feature import seed_feature_types
from reimagina.db import connect
from reimagina.jobs.overlap import compute_all_pairs, declare_pair, list_pairs


def declare_overlap_pair(
    *,
    dsn: str,
    source_context: str,
    source_slug: str,
    target_context: str,
    target_slug: str,
    auto_seed_types: bool = True,
) -> dict[str, object]:
    with connect(dsn) as conn:
        if auto_seed_types:
            seed_feature_types(conn)
        pair = declare_pair(
            conn,
            source_context=source_context,
            source_slug=source_slug,
            target_context=target_context,
            target_slug=target_slug,
        )
    return {"dsn": dsn, "pair": pair}


def list_overlap_pairs(*, dsn: str) -> dict[str, object]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "pairs": list_pairs(conn)}


def compute_overlaps_all(*, dsn: str, aoi_id: str | None = None) -> dict[str, object]:
    with connect(dsn) as conn:
        return {"dsn": dsn, **compute_all_pairs(conn, aoi_id=aoi_id)}
