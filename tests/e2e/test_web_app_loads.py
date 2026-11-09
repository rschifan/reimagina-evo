"""Every page of the web app opens, reaches the API, and raises nothing.

The gap this closes, stated plainly because it is the reason the suite
exists: the entire frontend stack — svelte, vite, vitest, jsdom, terra-draw,
tailwind-variants — was upgraded in one commit on the strength of
`npm run build` passing and 73 jsdom unit tests passing. Neither opens a
browser. A build that succeeds proves the modules type-check and bundle; it
says nothing about whether the page renders, and an upgrade that breaks the
map breaks it at runtime or not at all.

No pixel assertions. A screenshot comparison fails on a font substitution and
passes on a map drawing the wrong city, which is the wrong sensitivity in
both directions. These assert what the page reports about itself.
"""

from __future__ import annotations

import pytest

PAGES = ("map", "catalog", "ops")


@pytest.mark.parametrize("page", PAGES)
def test_the_page_loads(probe, page):
    seen = probe["pages"][page]
    assert seen["navigation"] is None, f"navigation failed: {seen['navigation']}"
    assert seen["title"], "the page has no title, which means it did not render"


@pytest.mark.parametrize("page", PAGES)
def test_the_page_raises_no_console_error(probe, page):
    """The signal a bundler upgrade actually moves.

    `maplibre-gl` 6 shipped its worker as a separate module, and the symptom
    was a console error on a page whose build was perfectly green. Nothing
    except a browser would have found it.
    """
    errors = probe["pages"][page]["consoleErrors"]
    assert not errors, "console errors on /" + page + ":\n  " + "\n  ".join(errors[:8])


@pytest.mark.parametrize("page", PAGES)
def test_the_page_reached_the_api(probe, page):
    """A page that renders its shell against a dead API looks fine.

    Every one of these pages is a thin client over `/indicators`, `/aois`,
    `/tile-sources` or `/jobs`. Rendering proves the bundle works; it does not
    prove the page has anything in it.
    """
    # ⚠ Matched on the path, not the origin. The client calls `/api/...`
    # same-origin and Vite proxies it to the backend — the mapping a reverse
    # proxy performs in production. Asserting on `:8001` passed only by
    # accident of where the API happened to run, and failed here against a
    # correctly configured app.
    requests = probe["pages"][page]["requests"]
    api_calls = [u for u in requests if "/api/" in u]
    assert api_calls, f"/{page} made no API request at all: {requests[:5]}"


def test_the_map_asks_for_tiles_in_the_documented_shape(probe):
    """The tile contract, as the client actually spells it.

    `serving_geo.get_tile(z, x, y, query_params)` is published over HTTP by
    the tile server, and the client has to call it with the path and
    parameters that function expects. This asserts the request, not the
    response — the response needs a tile server, and whether one is running is
    a deployment question rather than a property of the app.
    """
    tiles = [u for u in probe["pages"]["map"]["requests"] if "/get_tile/" in u]
    assert tiles, "the map requested no vector tiles"
    assert any("type_slug=" in u for u in tiles), (
        "no tile request carried a type_slug filter, so the layer toggles are "
        f"not reaching the tile function: {tiles[:3]}"
    )


def test_a_missing_tile_server_is_distinguishable_from_a_broken_app(probe):
    """The two states look identical in a browser and must not here.

    An empty map means either the app is broken or nothing is serving tiles.
    Every other suite reports both the same way — as nothing at all — which is
    how a frontend stack was upgraded with the map untested. Failed requests
    that are *all* to the tile server say "deployment"; a failed request
    anywhere else says "app".
    """
    failed = probe["pages"]["map"]["failedRequests"]
    not_tiles = [u for u in failed if "/get_tile/" not in u and "_tile/" not in u]
    assert not not_tiles, (
        "requests failed that were not tile requests, so this is the app "
        "rather than a missing tile server:\n  " + "\n  ".join(not_tiles[:8])
    )


def test_the_catalog_lists_the_shipped_indicators(probe):
    """The page is a view of the real catalogue, not a fixture.

    Checked by family rather than by slug: the families are a product
    decision that changes rarely, while slugs change whenever an indicator is
    renamed, and a test that fails on a rename teaches people to delete it.
    """
    text = probe["pages"]["catalog"]["text"].lower()
    for family in ("accessibility", "edificability", "livability", "urban"):
        assert family in text, f"the catalog page does not mention {family}"
