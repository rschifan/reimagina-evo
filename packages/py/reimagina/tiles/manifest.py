"""Tile layer classification manifest for live MVT and PMTiles snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reimagina.paths import REPO_ROOT


@dataclass(frozen=True)
class TileLayerDefinition:
    layer_id: str
    logical_name: str
    description: str
    source_mode: str
    implemented: bool
    source_relation: str | None = None
    tile_layer_name: str | None = None
    tileset_type: str = "overlay"
    minzoom: int | None = None
    maxzoom: int | None = None
    query_params: dict[str, Any] = field(default_factory=dict)


def _repo_root() -> Path:
    return REPO_ROOT


def default_manifest_path() -> Path:
    return _repo_root() / "infra" / "tiles" / "layers.json"


def load_tile_layers(
    manifest_path: str | Path | None = None,
) -> list[TileLayerDefinition]:
    path = Path(manifest_path) if manifest_path else default_manifest_path()
    payload = json.loads(path.read_text())
    layers = [
        TileLayerDefinition(
            layer_id=row["layer_id"],
            logical_name=row["logical_name"],
            description=row["description"],
            source_mode=row["source_mode"],
            implemented=bool(row.get("implemented", True)),
            source_relation=row.get("source_relation"),
            tile_layer_name=row.get("tile_layer_name"),
            tileset_type=row.get("tileset_type", "overlay"),
            minzoom=row.get("minzoom"),
            maxzoom=row.get("maxzoom"),
            query_params=row.get("query_params", {}),
        )
        for row in payload["layers"]
    ]
    return layers


def get_tile_layer(
    layer_id: str, manifest_path: str | Path | None = None
) -> TileLayerDefinition:
    for layer in load_tile_layers(manifest_path):
        if layer.layer_id == layer_id:
            return layer
    raise ValueError(f"Unknown tile layer: {layer_id!r}")
