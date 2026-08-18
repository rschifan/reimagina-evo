from __future__ import annotations

from pathlib import Path

from reimagina.tiles.manifest import get_tile_layer, load_tile_layers
from reimagina.tiles.export import (
    _postgres_ogr_connection_string,
    default_pmtiles_output_dir,
)
from reimagina.tiles.sources import _strip_aoi_token


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
    value = _postgres_ogr_connection_string(
        "postgresql://user:pass@localhost:5432/reimagina"
    )

    assert "host='localhost'" in value
    assert "dbname='reimagina'" in value
    assert "user='user'" in value
    assert "password='pass'" in value


def test_default_pmtiles_output_dir_points_inside_repo():
    assert default_pmtiles_output_dir() == Path.cwd() / "data" / "tiles" / "pmtiles"


# ── AOI-shortcode label stripping ────────────────────────────────────


def test_strip_aoi_token_removes_segment_when_it_matches_aoi_tokens():
    """The picker label should drop the AOI shortcode operators embed
    in attribute_key_prefix to dodge SCD prefix collisions —
    ``pop_worldpop_2020_cdmx_sum`` reads as ``pop_worldpop_2020_sum``
    once the resolver has mapped the key's affinity to a single AOI."""
    assert (
        _strip_aoi_token(
            "pop_worldpop_2020_cdmx_sum",
            ("cdmx", "all"),
        )
        == "pop_worldpop_2020_sum"
    )


def test_strip_aoi_token_handles_short_aoi_prefix_form():
    """Operators sometimes shorten the AOI name (``Mexicali`` →
    ``mex``). The 3-char prefix expansion in :func:`_aoi_display_tokens`
    surfaces ``mex`` as a recognised token alongside ``mexicali``, so
    ``_mex_sum`` strips just like ``_mexicali_sum`` would."""
    assert (
        _strip_aoi_token(
            "pop_worldpop_2020_mex_sum",
            ("mexicali", "mex"),
        )
        == "pop_worldpop_2020_sum"
    )


def test_strip_aoi_token_leaves_key_unchanged_when_segment_doesnt_match():
    """Census keys (``censo_2020_pobtot``) have no AOI shortcode and
    must come through untouched — accidentally trimming ``2020`` or
    ``pobtot`` because of a coincidental AOI-token match would corrupt
    every multi-AOI metric label."""
    assert (
        _strip_aoi_token(
            "censo_2020_pobtot",
            ("cdmx", "all"),
        )
        == "censo_2020_pobtot"
    )


def test_strip_aoi_token_no_op_on_two_segment_keys():
    """Pathological short keys (``cdmx_sum``) collapse below 2
    segments after a strip — return as-is to avoid producing the bare
    ``sum``."""
    assert _strip_aoi_token("cdmx_sum", ("cdmx",)) == "cdmx_sum"


def test_strip_aoi_token_no_op_on_empty_aoi_tokens():
    """Multi-AOI overlays carry no per-AOI tokens. The resolver gates
    on ``len(aois) == 1`` before calling this helper, but the helper
    must still no-op cleanly when callers pass ``()`` defensively."""
    assert _strip_aoi_token("any_key", ()) == "any_key"
