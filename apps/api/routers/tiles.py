"""Tile-source manifest endpoint — resolves the layer manifest + snapshot state.

Mirrors the JSON shape the CLI already produces via `tiles sources`, so the
frontend can consume one endpoint and get back fully-qualified tilejson /
pmtiles URLs without reading the raw manifest file itself.
"""

from __future__ import annotations

import threading
import time

import psycopg2
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from reimagina.tiles.sources import resolve_tile_sources

from ..deps import (
    get_db_conn,
    tile_live_base_url,
    tile_raster_base_url,
    tile_snapshot_base_url,
)

router = APIRouter(prefix="/tile-sources", tags=["tiles"])


# Process-local TTL cache for the resolver result. The resolver does
# 1+ s of GROUP BY scans + a DISTINCT join on the observation table —
# none of which change on operator-relevant timescales. A 30 s TTL
# means a page-reload during the same session is a memory hit, but
# operators who just ran an ingest still see the new layer within a
# minute. Keyed by the (live, snapshot, raster) base-URL tuple in
# case different consumers configure different bases.
_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_cache: dict[tuple[str | None, str | None, str | None], tuple[float, list]] = {}


class TileSource(BaseModel):
    layer_id: str
    logical_name: str | None = None
    description: str | None = None
    source_mode: str
    implemented: bool
    available: bool
    source_config: dict
    latest_snapshot: dict | None = None


@router.get("", response_model=list[TileSource])
def list_tile_sources(
    live_tilejson_base_url: str | None = Query(
        default=None,
        description=(
            "Base URL Martin exposes the tile function at. "
            "Defaults to REIMAGINA_TILE_LIVE_BASE_URL when omitted."
        ),
    ),
    snapshot_base_url: str | None = Query(
        default=None,
        description=(
            "Static host serving PMTiles snapshot artifacts. "
            "Defaults to REIMAGINA_TILE_SNAPSHOT_BASE_URL when omitted."
        ),
    ),
    raster_base_url: str | None = Query(
        default=None,
        description=(
            "Base URL of the titiler COG endpoint (e.g. "
            "http://localhost:8001/cog for the in-process mount). "
            "Defaults to REIMAGINA_TILE_RASTER_BASE_URL when omitted."
        ),
    ),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[TileSource]:
    """Resolve every tile layer (vector manifest, PMTiles snapshot, raster
    layer registry) into a frontend-ready source."""
    cache_key = (
        live_tilejson_base_url or tile_live_base_url(),
        snapshot_base_url or tile_snapshot_base_url(),
        raster_base_url or tile_raster_base_url(),
    )
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        rows = cached[1]
    else:
        rows = resolve_tile_sources(
            conn,
            live_tilejson_base_url=cache_key[0],
            snapshot_base_url=cache_key[1],
            raster_base_url=cache_key[2],
        )
        with _cache_lock:
            _cache[cache_key] = (now, rows)
    return [
        TileSource(
            layer_id=row["layer_id"],
            logical_name=row.get("logical_name"),
            description=row.get("description"),
            source_mode=row["source_mode"],
            implemented=row["implemented"],
            available=row["available"],
            source_config=row["source_config"],
            latest_snapshot=row.get("latest_snapshot"),
        )
        for row in rows
    ]
