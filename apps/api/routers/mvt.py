"""Live vector tiles, served by the API itself.

Martin is the recommended production path and stays the default. This is the
same contract served from this process, so a deployment can drop a service
without the frontend, the layer manifest or the tile-source resolver knowing
anything changed.

**It is not a new URL shape.** Martin's function-source convention —
``/{function}/{z}/{x}/{y}``, query string forwarded as ``query_params`` — is
the contract, and the three tile functions in ``serving_geo`` are written to
it. Mounted at ``/tiles``, this router serves the identical shape, so the
whole switch is one environment variable::

    REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:8001/tiles   # this router
    REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000         # Martin

``tiles.sources._martin_base_and_default_function`` already resolves a base
whose last segment is not a known function name by keeping the whole URL and
defaulting to ``get_tile``, which is exactly what the first line needs. There
is a contract test asserting both forms resolve to the same URL shape,
because "one variable" is the claim this module has to keep.

**This is the pattern the repository already chose, not a new one.**
``apps/api/app.py`` mounts titiler's COG endpoints inside the API under
``/cog`` so that "the API + tile server share a process, a dep tree, and an
origin", calling a sidecar "a later scaling decision". Raster tiles were
served here and vector tiles needed a separate service; that asymmetry had
history behind it rather than a reason.

**Why Martin still exists.** Measured on this repository's synthetic
territory, ``serving_geo.get_tile`` answers a populated z16 tile in ~1.5 ms,
so Postgres is not the constraint at that scale and neither is FastAPI. At
city scale with many clients panning at once, a Rust tile server with its own
pool in front of Postgres is the better machine, and nothing here claims
otherwise. The point is that it becomes an optimisation rather than a
dependency.

**What is actually hard here is the HTTP, not the SQL.** The tile bytes are
one function call. Getting a tile client to behave well needs the empty-tile
status, a validator it can revalidate against, and bounds rejected before
they reach the database — and each of those is a decision this module records
at the point it is made.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from psycopg2 import sql

from reimagina.tiles.functions import (
    MAX_ZOOM,
    TILE_FUNCTIONS,
    is_tile_function,
    tile_coordinates_valid,
)

from ..deps import get_db_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tiles", tags=["vector-tiles"])

#: The MIME type registered for Mapbox Vector Tiles. MapLibre does not
#: require it, but shared caches and CDNs route on it, and serving tiles as
#: ``application/octet-stream`` is the kind of detail that works locally and
#: is diagnosed a week later.
MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

#: Below this, compression costs more than it saves: gzip's own header and
#: trailer are 18 bytes, and MVT is already a packed protobuf with
#: varint-delta geometry. Measured on a populated z16 tile here, gzip -6 buys
#: 1.86x (4,101 B -> 2,203 B) — real, but not the 5x people expect from text,
#: which is why `ETag` below matters more than this does.
GZIP_MIN_BYTES = 1024

#: Compression level. 6 is zlib's default and the knee of the curve; 9 costs
#: noticeably more CPU on a hot path for a percent or two of size.
GZIP_LEVEL = 6


def _cache_control() -> str:
    """How long a tile may be reused, and for how long while revalidating.

    Tiles derive from mutable data, so this is deliberately short. The second
    directive is the one that matters for a map: ``stale-while-revalidate``
    lets a client paint an almost-current tile immediately and fetch a fresh
    one behind it, which is the difference between a map that pans and a map
    that blinks.

    ``public`` is correct even for scenario overlays. A scenario tile varies
    by ``scenario_id``, which is in the query string, so a shared cache
    already keys on it; nothing served here is per-user, and this API has no
    authentication to leak across.
    """
    max_age = os.environ.get("REIMAGINA_TILE_MAX_AGE_S", "60")
    swr = os.environ.get("REIMAGINA_TILE_STALE_WHILE_REVALIDATE_S", "300")
    return f"public, max-age={max_age}, stale-while-revalidate={swr}"


def _statement_timeout_ms() -> int:
    """Per-request ceiling on a tile query.

    A tile request is the one endpoint a client issues dozens of at a time, so
    a pathological tile — a zoom that defeats the simplification, an overlay
    over a huge AOI — must fail its own request rather than hold a pooled
    connection while the browser retries.
    """
    return int(os.environ.get("REIMAGINA_TILE_TIMEOUT_MS", "5000"))


def _etag(payload: bytes, *, encoded: bool) -> str:
    """A strong validator for these bytes, in this encoding.

    ⚠ The encoding is part of the tag, and that is not fussiness. A shared
    cache that stored a gzip body under an identity tag would later serve
    those bytes to a client that did not send ``Accept-Encoding: gzip``, which
    is an unreadable tile and a bug no one can reproduce locally. Pairing the
    tag with the encoding, plus ``Vary: Accept-Encoding`` on the response, is
    what keeps that impossible.

    Truncated to 128 bits: this is a cache validator, not a signature, and a
    shorter tag is a smaller header on a response measured in kilobytes.
    """
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f'"{digest}-gzip"' if encoded else f'"{digest}"'


def _query_params(request: Request) -> str:
    """The query string, as the JSON the tile functions expect.

    Forwarded wholesale, exactly as Martin does, because the functions
    themselves decide which keys mean anything — ``type_slug`` and ``role``
    for ``get_tile``; ``indicator_slug``, ``scenario_id``, ``feature_type``
    and ``aoi_id`` for the overlays. Filtering here would mean this module
    needs updating every time a migration teaches a function a new parameter,
    which is the coupling the shared signature exists to avoid.

    Repeated parameters keep the last value, matching a query-string
    convention the SQL functions could not express anyway: they read
    ``query_params->>'key'``, which is a single value.
    """
    return json.dumps(dict(request.query_params))


@router.get(
    "/{function}/{z}/{x}/{y}",
    responses={
        200: {"content": {MVT_MEDIA_TYPE: {}}, "description": "A tile with features"},
        204: {"description": "A valid tile with nothing in it"},
        304: {"description": "The client's cached copy is current"},
        404: {"description": "Not a servable tile function"},
        422: {"description": "The coordinate addresses no tile that can exist"},
    },
    response_class=Response,
    summary="Live MVT tile",
)
def get_vector_tile(  # noqa: PLR0913 — every parameter is part of the URL contract
    request: Request,
    function: Annotated[str, Path(description="A function in `serving_geo`")],
    z: Annotated[int, Path(description="Zoom")],
    x: Annotated[int, Path(description="Tile column")],
    y: Annotated[int, Path(description="Tile row")],
    conn: Annotated[Any, Depends(get_db_conn)],
    accept_encoding: Annotated[str | None, Header()] = None,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """Serve one tile from a `serving_geo` tile function."""
    if not is_tile_function(function):
        # 404 rather than 422: from a client's point of view the resource does
        # not exist, and naming the valid set is more useful than a schema
        # error. The set is public — these are world-readable tile layers —
        # so listing it leaks nothing a `/tile-sources` call would not.
        raise HTTPException(
            status_code=404,
            detail=(
                f"{function!r} is not a tile function. "
                f"Available: {', '.join(sorted(TILE_FUNCTIONS))}"
            ),
        )
    if not tile_coordinates_valid(z, x, y):
        raise HTTPException(
            status_code=422,
            detail=(
                f"z/x/y {z}/{x}/{y} is not a tile that can exist: z must be "
                f"0..{MAX_ZOOM} and x, y must be 0..{(1 << z) - 1 if 0 <= z <= MAX_ZOOM else '2^z-1'}"
            ),
        )

    payload = _fetch_tile(conn, function, z, x, y, _query_params(request))

    if not payload:
        # ⚠ Empty, not absent, and the distinction is the whole reason this
        # branch is commented. `ST_AsMVT` over an empty set returns
        # zero-length bytes, **not** NULL — verified against the database:
        # `serving_geo.get_tile(14,0,0)` gives `length = 0, IS NULL = false`.
        # So a route testing for NULL would answer 200 with an empty body
        # forever.
        #
        # 204 is also the right answer rather than 404. The tile exists and is
        # legitimately empty — ocean, or outside the loaded territory — and
        # 404 would make a map log an error for every tile beyond the data,
        # which is most of the world. MapLibre treats both as "nothing to
        # draw"; only one of them is true.
        return Response(status_code=204, headers={"Cache-Control": _cache_control()})

    wants_gzip = "gzip" in (accept_encoding or "").lower()
    compress = wants_gzip and len(payload) >= GZIP_MIN_BYTES
    body = gzip.compress(payload, GZIP_LEVEL) if compress else payload
    etag = _etag(payload, encoded=compress)

    headers = {
        "ETag": etag,
        "Cache-Control": _cache_control(),
        # Paired with the encoding-specific ETag above. Without it a shared
        # cache may serve either body under either tag.
        "Vary": "Accept-Encoding",
    }
    if compress:
        headers["Content-Encoding"] = "gzip"

    if if_none_match and etag in {tag.strip() for tag in if_none_match.split(",")}:
        # The single highest-value optimisation on this path, and the reason
        # compression is the lesser one: gzip buys 1.86x, a 304 buys 100%. A
        # panning client revisits tiles constantly.
        return Response(status_code=304, headers=headers)

    logger.debug(
        "tile %s/%d/%d/%d -> %dB%s",
        function,
        z,
        x,
        y,
        len(body),
        " gzip" if compress else "",
    )
    return Response(content=body, media_type=MVT_MEDIA_TYPE, headers=headers)


def _fetch_tile(
    conn: Any, function: str, z: int, x: int, y: int, query_params: str
) -> bytes:
    """Call one tile function, read-only and time-boxed.

    ``sql.Identifier`` rather than an f-string even though ``function`` has
    already been checked against ``TILE_FUNCTIONS``. The check is the
    security boundary; this is what keeps a future mistake in the registry
    from becoming an injection rather than a 404.

    Read-only because every tile function is declared ``STABLE``, so the
    transaction costs nothing and removes a category of accident outright —
    including a future function added to the registry that should not have
    been.
    """
    statement = sql.SQL("SELECT {fn}(%s, %s, %s, %s::json)").format(
        fn=sql.Identifier("serving_geo", function)
    )
    with conn.cursor() as cur:
        # ⚠ A savepoint, not the whole transaction, and the reason is that
        # `SET LOCAL` is transaction-scoped rather than statement-scoped.
        #
        # The first version set `transaction_read_only = on` on the ambient
        # transaction and then called `conn.rollback()`. Correct for a
        # request that owns its connection, which is how the pool hands one
        # out — and quietly destructive otherwise: anything sharing that
        # transaction afterwards failed with `cannot execute INSERT in a
        # read-only transaction`, and a plain `rollback()` would have thrown
        # away a caller's uncommitted work besides. The integration suite,
        # which pins the API to its own seeding transaction, found it on the
        # first run.
        #
        # Postgres stacks GUC settings per subtransaction, so
        # `ROLLBACK TO SAVEPOINT` undoes both `SET LOCAL`s and leaves an
        # outer transaction untouched. Two extra statements buy a tile read
        # that is genuinely self-contained: safe on a pooled connection, safe
        # inside a caller's unit of work, and read-only either way.
        cur.execute("SAVEPOINT reimagina_tile")
        try:
            # Read-only costs nothing — every tile function is declared
            # STABLE — and removes a category of accident outright,
            # including a function added to the registry that should not
            # have been.
            cur.execute("SET LOCAL transaction_read_only = on")
            cur.execute("SET LOCAL statement_timeout = %s", (_statement_timeout_ms(),))
            cur.execute(statement, (z, x, y, query_params))
            row = cur.fetchone()
        finally:
            # In `finally` so a timeout or a bad parameter releases the
            # read-only setting too; otherwise one failed tile would leave
            # the rest of the transaction unable to write.
            cur.execute("ROLLBACK TO SAVEPOINT reimagina_tile")
            cur.execute("RELEASE SAVEPOINT reimagina_tile")
    if row is None or row[0] is None:
        return b""
    return bytes(row[0])


@router.get(
    "/{function}",
    summary="TileJSON for a live MVT tile function",
    responses={404: {"description": "Not a servable tile function"}},
)
def get_tilejson(
    request: Request,
    function: Annotated[str, Path(description="A function in `serving_geo`")],
) -> dict[str, object]:
    """TileJSON 3.0.0 for one tile function.

    Here so this router is drop-in for the resolver's ``tilejson_url`` and not
    only for its ``tiles_url_template``. Martin serves TileJSON at the
    function's own path, so without this, half the manifest would point at
    something that 404s and the asymmetry would surface as a frontend bug
    rather than a missing endpoint.

    The tile URL is built from the incoming request rather than from
    configuration, so a deployment behind a reverse proxy or a different port
    advertises the URL the client actually reached, not the one the server
    thinks it has.
    """
    if not is_tile_function(function):
        raise HTTPException(
            status_code=404,
            detail=(
                f"{function!r} is not a tile function. "
                f"Available: {', '.join(sorted(TILE_FUNCTIONS))}"
            ),
        )
    base = str(request.url_for("get_vector_tile", function=function, z=0, x=0, y=0))
    template = base.rsplit("/0/0/0", 1)[0] + "/{z}/{x}/{y}"
    return {
        "tilejson": "3.0.0",
        "name": function,
        "scheme": "xyz",
        "tiles": [template],
        "minzoom": 0,
        # Not MAX_ZOOM: that is the coordinate validator's ceiling, which is
        # about what can be addressed. 22 is the deepest any basemap in use
        # goes, and advertising more would have clients request tiles that
        # are valid and always empty.
        "maxzoom": 22,
        "vector_layers": [{"id": "features", "fields": {}}],
    }
