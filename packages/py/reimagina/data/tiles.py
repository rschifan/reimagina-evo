"""Tile snapshot metadata and hybrid source resolution helpers."""
from __future__ import annotations

from reimagina.storage import backend_for_connection
from reimagina.tiles.service import resolve_tile_sources


def list_tile_snapshots(conn, *, layer_id: str | None = None) -> list[dict]:
    return backend_for_connection(conn).tile_repo().list_snapshots(layer_id=layer_id)


def get_latest_tile_snapshot(conn, *, layer_id: str) -> dict | None:
    return backend_for_connection(conn).tile_repo().get_latest_snapshot(layer_id=layer_id)


def get_tile_sources(
    conn,
    *,
    live_tilejson_base_url: str = "http://localhost:3000/get_tile",
    snapshot_base_url: str | None = None,
    manifest_path: str | None = None,
) -> list[dict]:
    return resolve_tile_sources(
        conn,
        live_tilejson_base_url=live_tilejson_base_url,
        snapshot_base_url=snapshot_base_url,
        manifest_path=manifest_path,
    )
