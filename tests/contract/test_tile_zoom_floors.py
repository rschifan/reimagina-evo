"""The road zoom floor is one number, held in two languages.

``get_tile`` (r057) serves roads from ``ROAD_MIN_ZOOM``; the web street layer
declares its own ``minzoom``. A client floor below the server's buys requests
that can only come back empty — on a map, indistinguishable from missing data
— and one above hides roads the server would serve. Nothing else compares them.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _r057():
    path = ROOT / "infra/db/migrations/versions/r057_tile_road_min_zoom.py"
    spec = importlib.util.spec_from_file_location("_r057", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _web_road_minzoom() -> int:
    text = (ROOT / "apps/web/src/layers.ts").read_text()
    entry = text[text.index('typeSlug: "road"') :]
    entry = entry[: entry.index("\n  },")]
    return int(re.findall(r"^\s*minzoom: (\d+),", entry, re.M)[-1])


def test_roads_start_at_z13():
    assert _r057().ROAD_MIN_ZOOM == 13


def test_the_floor_is_in_the_function_body():
    r057 = _r057()
    body = r057.get_tile_body()
    assert f"AND (z >= {r057.ROAD_MIN_ZOOM} OR f.type_slug <> 'road')" in body
    # r039's AOI filter and r038's building floor survive.
    assert "AND (z >= 14 OR f.type_slug <> 'building')" in body
    assert "curated_geo.aoi_feature" in body


def test_the_web_street_layer_asks_from_the_same_zoom():
    assert _web_road_minzoom() == _r057().ROAD_MIN_ZOOM
