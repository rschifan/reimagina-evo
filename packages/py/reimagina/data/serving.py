"""Query helpers for serving_geo views.

Workers and the API must only read through these views — never from curated_geo directly.
"""
from __future__ import annotations
from reimagina.storage import backend_for_connection

def get_features(
    conn,
    *,
    type_slug: str | None = None,
    role: str | None = None,
    aoi_id: str | None = None,
    limit: int = 500,
    after_cursor: str | None = None,
) -> dict:
    """Query serving_geo.feature_v with optional type/role/AOI filters.

    When aoi_id is provided, results are joined to aoi_feature and include overlap_ratio.

    Returns {"features": list[dict], "next_cursor": str | None}.
    Pass next_cursor as after_cursor in the next call to page forward.
    """
    return backend_for_connection(conn).serving_reader().get_features(
        type_slug=type_slug,
        role=role,
        aoi_id=aoi_id,
        limit=limit,
        after_cursor=after_cursor,
    )
