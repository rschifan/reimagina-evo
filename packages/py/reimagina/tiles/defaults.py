"""Single source of truth for tile-service URL defaults.

The API layer, the CLI, and tile-related helpers all need the same
localhost fallbacks. Keeping them in one module means a host change at
deploy time edits one line, not four.

Resolved at call time so env-var changes (or test monkeypatches) take
effect without reloading the module.
"""
from __future__ import annotations

import os


def tile_live_base_url() -> str:
    """Base URL Martin exposes the live tile function at."""
    return os.environ.get(
        "REIMAGINA_TILE_LIVE_BASE_URL",
        "http://localhost:3000/get_tile",
    )


def tile_snapshot_base_url() -> str:
    """Static host serving PMTiles snapshot artifacts."""
    return os.environ.get(
        "REIMAGINA_TILE_SNAPSHOT_BASE_URL",
        "http://localhost:3001",
    )
