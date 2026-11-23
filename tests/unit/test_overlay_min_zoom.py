"""The overlay floor the tile-source config advertises matches the SQL one."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reimagina.tiles.sources import OVERLAY_MIN_ZOOM

MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/db/migrations/versions"


@pytest.mark.parametrize(
    "function", ["aggregation_overlay_tile", "indicator_overlay_tile"]
)
def test_the_advertised_floor_is_the_tile_functions_floor(function):
    """Read the floor from the latest migration that defines the function.

    The first ``AND z >= N`` in that file is its upgrade body; later
    occurrences can be downgrade copies with an older floor.
    """
    definition = f"CREATE OR REPLACE FUNCTION serving_geo.{function}"
    latest = max(
        (p for p in MIGRATIONS.glob("r*.py") if definition in p.read_text()),
        key=lambda p: p.name,
    )
    floor = re.search(r"AND z >= (\d+)", latest.read_text())
    assert floor is not None, latest.name
    assert int(floor.group(1)) == OVERLAY_MIN_ZOOM, latest.name
