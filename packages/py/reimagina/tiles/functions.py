"""The tile functions this platform serves, and the rules for naming one.

One list, read by everything that needs it. ``tiles.sources`` uses it to
decide whether a URL's final segment is a function name or part of a path
prefix; ``apps.api.routers.mvt`` uses it as the allowlist of what it will
call. Those two must agree — a function the resolver advertises but the API
refuses to serve is a 404 in a map, and a function the API serves but the
resolver never names is dead code — and the only way to keep two lists in
step is to have one.

**Why an allowlist at all.** A route that takes a function name out of a URL
and calls it is an RPC endpoint into Postgres. This repository already
refused that once, in ``cog_security.validated_cog_path``: titiler's default
path dependency accepts any ``url`` query parameter and hands it to
rasterio/GDAL, so ``/cog/info?url=file:///etc/passwd`` would read host files.
A tile function name is the same hazard wearing different clothes — Postgres
has plenty of functions that return ``bytea``, and some of them are
``lo_get``.

So the name is never data. It is matched against this set, and the call is
then built with ``psycopg2.sql.Identifier`` rather than interpolation, so a
mistake in the set cannot become an injection.

**Why the signature is the contract.** Every function here takes
``(z integer, x integer, y integer, query_params json)`` and returns
``bytea``. That is Martin's function-source convention, not ours — Martin's
entire job is to expose exactly that signature at ``/{fn}/{z}/{x}/{y}`` and
forward the query string as ``query_params``. Because the signature is the
contract rather than the server, a second implementation of it (the API's own
MVT route) needs no new concepts and no new URL shape. Keep any function
added here to that signature, or the two servers stop being interchangeable.
"""

from __future__ import annotations

from typing import Final

#: Functions in ``serving_geo`` that may be served over HTTP as vector tiles.
#:
#: Ordered by how they are used rather than alphabetically: the baseline
#: layer, then the two overlays.
#:
#: ⚠ Adding a name here makes it publicly reachable at
#: ``/tiles/<name>/{z}/{x}/{y}`` and at Martin's equivalent path. Add one only
#: for a function that is meant to be world-readable, takes the signature in
#: this module's docstring, and is ``STABLE`` — a ``VOLATILE`` function
#: reachable by GET is a write primitive behind a cache.
TILE_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "get_tile",
        "indicator_overlay_tile",
        "aggregation_overlay_tile",
    }
)

#: The function assumed when a base URL names none. Historically every live
#: layer was the baseline layer, and ``layers.json`` still leaves
#: ``source_relation`` unset for it.
DEFAULT_TILE_FUNCTION: Final[str] = "get_tile"

#: Web Mercator's practical zoom ceiling. Past 24 the tile grid exceeds what
#: a 32-bit tile index can address and ``ST_TileEnvelope`` stops being
#: meaningful; no basemap in use goes beyond 22.
MAX_ZOOM: Final[int] = 24


def is_tile_function(name: str) -> bool:
    """Whether ``name`` may be served as a tile function."""
    return name in TILE_FUNCTIONS


def tile_coordinates_valid(z: int, x: int, y: int) -> bool:
    """Whether ``(z, x, y)`` addresses a tile that can exist.

    Checked before the database is touched. ``ST_TileEnvelope`` accepts
    nonsense cheerfully — it will hand back an envelope for ``x`` far outside
    the grid, and the query then scans for features that cannot be there — so
    a client walking ``z=30`` turns a typo into database load. Rejecting the
    coordinate is both the correct HTTP answer and the cheap one.
    """
    if z < 0 or z > MAX_ZOOM:
        return False
    limit = 1 << z
    return 0 <= x < limit and 0 <= y < limit
