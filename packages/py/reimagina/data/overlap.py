"""Compute and query feature-to-feature spatial overlaps.

The feature_overlap table generalises aoi_feature to feature×feature, enabling
cross-grain indicator aggregation for both hierarchical and non-hierarchical
spatial relationships.  See docs/architecture/DATA_LAYER.md.
"""
from __future__ import annotations

from reimagina.storage import backend_for_connection


def compute_feature_overlaps(
    conn,
    source_type_id: str,
    target_type_id: str,
    *,
    aoi_id: str | None = None,
) -> int:
    """Compute overlap rows for all intersecting pairs of the given feature types.

    Measures are geodesic: m² for polygon (area), m for line (length),
    dimensionless 1.0 for point (presence). When aoi_id is set, only source
    features intersecting that AOI are considered.

    Returns the number of rows written (upserted).
    """
    return (
        backend_for_connection(conn)
        .overlap_repo()
        .compute_feature_overlaps(source_type_id, target_type_id, aoi_id=aoi_id)
    )
