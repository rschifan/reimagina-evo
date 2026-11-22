"""/tile-sources caches only the env-default resolution.

The cache used to be keyed by the three free-form base-URL query params, so
every distinct value added an entry that was never evicted.
"""

from __future__ import annotations

import apps.api.routers.tiles as tiles


def test_override_params_bypass_the_cache(monkeypatch):
    calls: list[str | None] = []

    def fake_resolve(_conn, **kw):
        calls.append(kw["raster_base_url"])
        return []

    monkeypatch.setattr(tiles, "resolve_tile_sources", fake_resolve)
    monkeypatch.setattr(tiles, "_cache", {})

    for i in range(3):
        tiles.list_tile_sources(
            live_tilejson_base_url=None,
            snapshot_base_url=None,
            raster_base_url=f"http://x/{i}",
            conn=None,
        )
    assert tiles._cache == {}
    assert len(calls) == 3

    for _ in range(2):
        tiles.list_tile_sources(
            live_tilejson_base_url=None,
            snapshot_base_url=None,
            raster_base_url=None,
            conn=None,
        )
    assert len(calls) == 4  # second default call is a cache hit
    assert len(tiles._cache) == 1
