"""CLI helpers for hybrid live-MVT and PMTiles tile infrastructure."""

from __future__ import annotations

from reimagina.db import managed_connection
from reimagina.storage import backend_for_connection
from reimagina.tiles import (
    export_pmtiles_snapshot,
    load_tile_layers,
    resolve_tile_sources,
)


def tile_manifest(*, manifest_path: str | None = None) -> dict[str, object]:
    layers = [
        {
            "layer_id": layer.layer_id,
            "logical_name": layer.logical_name,
            "description": layer.description,
            "source_mode": layer.source_mode,
            "implemented": layer.implemented,
            "source_relation": layer.source_relation,
            "tile_layer_name": layer.tile_layer_name,
            "minzoom": layer.minzoom,
            "maxzoom": layer.maxzoom,
            "query_params": layer.query_params,
        }
        for layer in load_tile_layers(manifest_path)
    ]
    return {"layers": layers}


def export_tile_snapshot(
    *,
    dsn: str,
    layer_id: str,
    output_dir: str | None = None,
    snapshot_base_url: str | None = None,
    manifest_path: str | None = None,
) -> dict[str, object]:
    return export_pmtiles_snapshot(
        dsn=dsn,
        layer_id=layer_id,
        output_dir=output_dir,
        snapshot_base_url=snapshot_base_url,
        manifest_path=manifest_path,
    )


def list_tile_sources(
    *,
    dsn: str,
    live_tilejson_base_url: str,
    snapshot_base_url: str | None = None,
    manifest_path: str | None = None,
) -> dict[str, object]:
    with managed_connection(dsn) as conn:
        return {
            "dsn": dsn,
            "layers": resolve_tile_sources(
                conn,
                live_tilejson_base_url=live_tilejson_base_url,
                snapshot_base_url=snapshot_base_url,
                manifest_path=manifest_path,
            ),
        }


def list_tile_snapshots(*, dsn: str, layer_id: str | None = None) -> dict[str, object]:
    with managed_connection(dsn) as conn:
        snapshots = (
            backend_for_connection(conn).tile_repo().list_snapshots(layer_id=layer_id)
        )
    return {"dsn": dsn, "snapshots": snapshots}
