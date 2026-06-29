"""Integration-test fixtures shared across the suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_tile_source_caches():
    """Reset the routers' process-global resolver caches before each test.

    ``/tile-sources`` and ``/aois`` memoise their resolver output in
    module-level 30 s TTL caches (a deliberate production optimisation).
    A full integration run finishes well inside that TTL, so without a
    reset one test's response leaks into the next — a layer ingested in
    test B is masked by test A's cached rows. Clearing the caches per
    test restores isolation without touching the production behaviour.
    """
    import apps.api.routers.aois as aois_router
    import apps.api.routers.tiles as tiles_router

    tiles_router._cache.clear()
    aois_router._list_cache = None
    yield
