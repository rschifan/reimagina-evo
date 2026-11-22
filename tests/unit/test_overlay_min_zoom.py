"""The overlay floor the tile-source config advertises matches the SQL one."""

from __future__ import annotations

from pathlib import Path

from reimagina.tiles.sources import OVERLAY_MIN_ZOOM

MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/db/migrations/versions"


def test_the_advertised_floor_is_the_tile_functions_floor():
    """r025 lowered both overlay functions to ``z >= 8``; advertising 10 hid two
    zoom levels the server answers."""
    for name in ("r025", "r049"):
        (path,) = MIGRATIONS.glob(f"{name}_*.py")
        assert f"AND z >= {OVERLAY_MIN_ZOOM}" in path.read_text(), name
