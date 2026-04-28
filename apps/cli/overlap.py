"""CLI helpers for feature-to-feature overlap orchestration."""

from __future__ import annotations

from reimagina.db import connect
from reimagina.jobs.overlap import compute_all_pairs, declare_pair, list_pairs


def declare_overlap_pair(
    *,
    dsn: str,
    source_context: str,
    source_slug: str,
    target_context: str,
    target_slug: str,
) -> dict[str, object]:
    """Register a source→target feature-type pair in feature_overlap_type_pair.

    The feature types must already exist in curated_geo.feature_type_catalog.
    Use `reimagina-cli feat seed-types` first if the catalog is empty.
    """
    with connect(dsn) as conn:
        pair = declare_pair(
            conn,
            source_context=source_context,
            source_slug=source_slug,
            target_context=target_context,
            target_slug=target_slug,
        )
    return {"dsn": dsn, "pair": pair}


def list_overlap_pairs(*, dsn: str) -> dict[str, object]:
    """Return declared (source_type, target_type) pairs joined with catalog slugs."""
    with connect(dsn) as conn:
        return {"dsn": dsn, "pairs": list_pairs(conn)}


def compute_overlaps_all(*, dsn: str, aoi_id: str | None = None) -> dict[str, object]:
    """Run overlap compute across every declared type-pair.

    Pass `aoi_id` to scope the run; without it the compute iterates all
    source features and will be slow at city scale.
    """
    with connect(dsn) as conn:
        return {"dsn": dsn, **compute_all_pairs(conn, aoi_id=aoi_id)}
