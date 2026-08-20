"""Sub-AOI metrics — counts of features intersecting a user-drawn polygon.

The web app's selection tool (circle / rectangle / freehand polygon)
needs per-layer counts of features inside the drawn shape so the
operator can read "X manzanas selected, Y buildings selected, …".

The earlier client-side implementation walked
``map.queryRenderedFeatures`` + ``querySourceFeatures`` and ran a JS
point-in-polygon test. That undercounts whenever the renderer hasn't
loaded the relevant tiles (zoomed out, layer toggled off mid-drag),
and scales linearly with visible features in JS. PostGIS' ``ST_Intersects``
against the GiST-indexed ``serving_geo.feature.geometry`` column is the
authoritative answer — bounded by AOI so a Mexicali polygon can't
accidentally count CDMX features.

Endpoint contract is intentionally minimal: one POST with a polygon
and the type_slugs to count. Sum/mean reducers across feature
attributes are not exposed here — observation overlays carry those
through the indicator endpoints, so layering them onto this surface
would just duplicate logic. Add when a use case demands it.
"""

from __future__ import annotations

import json
import math
from uuid import UUID

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_backend, get_db_conn

router = APIRouter(prefix="/metrics", tags=["metrics"])


class WithinPolygonRequest(BaseModel):
    """Inputs for the within-polygon count."""

    # ``UUID``, not ``str``. ``feature_aoi_v.aoi_id`` is declared ``uuid``, so a
    # malformed value reached Postgres and returned
    # ``InvalidTextRepresentation`` — which is a ``DataError``, *not* the
    # ``InvalidParameterValue`` the handler below catches, so it fell through to
    # the generic 500. Pydantic rejects it as a 422 before any SQL runs.
    aoi_id: UUID = Field(
        ..., description="AOI scope — bounds the search to one city / area."
    )
    polygon: dict = Field(
        ...,
        description=(
            "GeoJSON Polygon (EPSG:4326). The polygon's outer ring defines the "
            "selection; holes are honoured by ST_Intersects."
        ),
    )
    type_slugs: list[str] = Field(
        ...,
        description=(
            "Feature types to count separately. Each slug appears in the "
            "response with its intersecting count; missing slugs resolve to 0 "
            "client-side."
        ),
    )


class WithinPolygonResultRow(BaseModel):
    type_slug: str
    count: int


class WithinPolygonResponse(BaseModel):
    results: list[WithinPolygonResultRow]


@router.post("/within-polygon", response_model=WithinPolygonResponse)
def count_within_polygon(
    payload: WithinPolygonRequest,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> WithinPolygonResponse:
    """Count features intersecting a GeoJSON polygon, grouped by type_slug."""
    if (payload.polygon.get("type") or "") != "Polygon":
        # Pydantic typed ``polygon`` as ``dict`` so structural shape
        # isn't validated. PostGIS would accept other GeoJSON types
        # (Point, etc.) and silently return 0 — confusing. Reject
        # non-Polygon at the boundary with a clear 422.
        raise HTTPException(
            status_code=422,
            detail="payload.polygon must be a GeoJSON Polygon "
            f"(got type={payload.polygon.get('type')!r}).",
        )
    if _polygon_has_non_finite_coords(payload.polygon):
        # PostGIS' ST_GeomFromGeoJSON silently accepts NaN / Inf
        # coordinates and produces a degenerate geometry that
        # ST_Covers rejects against everything (count → 0). That
        # surfaces as "selection valid but empty" — the wrong signal
        # for what is clearly malformed input. Reject explicitly.
        raise HTTPException(
            status_code=422,
            detail="polygon coordinates must be finite numbers (no NaN / Inf).",
        )
    polygon_json = json.dumps(payload.polygon)
    try:
        counts = (
            get_backend(conn)
            .serving_reader()
            .count_features_within_polygon(
                aoi_id=str(payload.aoi_id),
                polygon_geojson=polygon_json,
                type_slugs=list(payload.type_slugs),
            )
        )
    except psycopg2.errors.InvalidParameterValue as exc:
        # ST_GeomFromGeoJSON rejects malformed geometry (wrong type,
        # NaN coordinates, non-array structure). Treat as client error.
        raise HTTPException(status_code=422, detail=f"invalid polygon: {exc}") from exc

    return WithinPolygonResponse(
        results=[
            WithinPolygonResultRow(type_slug=slug, count=counts.get(slug, 0))
            for slug in payload.type_slugs
        ],
    )


def _polygon_has_non_finite_coords(polygon: dict) -> bool:
    """Return True if any coordinate in a GeoJSON Polygon is NaN / Inf."""
    rings = polygon.get("coordinates")
    if not isinstance(rings, list):
        return False  # Pydantic / earlier guards handle non-list shape
    for ring in rings:
        if not isinstance(ring, list):
            continue
        for pos in ring:
            if not isinstance(pos, list):
                continue
            for coord in pos:
                if isinstance(coord, (int, float)) and not math.isfinite(coord):
                    return True
    return False
