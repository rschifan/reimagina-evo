from __future__ import annotations

from pathlib import Path

from reimagina.tiles.manifest import get_tile_layer, load_tile_layers
from reimagina.tiles.service import _postgres_ogr_connection_string, default_pmtiles_output_dir


def test_tile_manifest_contains_expected_modes():
    layers = {layer.layer_id: layer for layer in load_tile_layers()}

    assert layers["admin_reference"].source_mode == "pmtiles_snapshot"
    assert layers["baseline_live"].source_mode == "live_mvt"
    assert layers["scenario_overlay"].source_mode == "live_mvt"
    assert layers["indicator_overlay"].source_mode == "live_mvt"


def test_tile_manifest_keeps_scenarios_and_indicators_live_only():
    for layer_id in ["scenario_overlay", "indicator_overlay"]:
        layer = get_tile_layer(layer_id)
        assert layer.source_mode != "pmtiles_snapshot"


def test_postgres_ogr_connection_string_supports_basic_uri():
    value = _postgres_ogr_connection_string("postgresql://user:pass@localhost:5432/reimagina")

    assert "host='localhost'" in value
    assert "dbname='reimagina'" in value
    assert "user='user'" in value
    assert "password='pass'" in value


def test_default_pmtiles_output_dir_points_inside_repo():
    assert default_pmtiles_output_dir() == Path.cwd() / "data" / "tiles" / "pmtiles"
