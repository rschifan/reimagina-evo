"""HTTP behaviour of the API-served MVT route, without a database.

The tile bytes are one function call; what is actually hard on this path is
the HTTP, and all of it is decidable without Postgres. So the status codes,
the validators and the encoding live here, and `tests/integration` asserts
that real bytes come back and decode.

See `docs/superpowers/specs/API_SERVED_MVT.md` for why this route exists
alongside Martin rather than instead of it.
"""

from __future__ import annotations

import gzip
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg2 import sql

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from apps.api.routers.mvt import GZIP_MIN_BYTES, MVT_MEDIA_TYPE
from reimagina.tiles.functions import TILE_FUNCTIONS

# A populated tile, big enough to cross the compression threshold, and
# incompressible enough that gzip cannot shrink it below. Random-looking
# bytes rather than zeros: a run of zeros compresses ~1000x, which would make
# the gzip assertions pass for the wrong reason.
BIG_TILE = bytes((i * 37 + (i >> 3)) % 251 for i in range(GZIP_MIN_BYTES * 3))
SMALL_TILE = b"\x1a\x20small-but-present"


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None) -> None:
        # The object, not `str()` of it: a `sql.Composed` stringifies to its
        # repr, so asserting on text here would assert on psycopg2's repr
        # format rather than on what was actually built.
        self._conn.statements.append((statement, params))

    def fetchone(self) -> tuple[Any] | None:
        return (self._conn.payload,)


class _Conn:
    """Returns one canned tile and records what was asked of it."""

    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.statements: list[tuple[Any, Any]] = []
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def tile_query(self) -> tuple[Any, Any]:
        """The composed tile call, found rather than assumed to be last.

        The route wraps the read in a savepoint, so the final statement is a
        `RELEASE SAVEPOINT`. Indexing from the end of the list made these
        tests depend on how many statements the route happens to issue, which
        is exactly the detail they should not care about.
        """
        composed = [
            entry for entry in self.statements if isinstance(entry[0], sql.Composed)
        ]
        assert len(composed) == 1, f"expected one tile query, got {composed}"
        return composed[0]

    @property
    def plain_sql(self) -> str:
        return " ".join(
            statement for statement, _ in self.statements if isinstance(statement, str)
        )


@pytest.fixture
def served():
    """A client whose tile payload the test chooses.

    `initialise_pool=False`: every `get_db_conn` is overridden, so no real
    pool is wanted. The app is still entered as a context manager because
    `create_app`'s lifespan is what the override replaces.
    """

    def _make(payload: bytes | None) -> tuple[TestClient, _Conn]:
        app = create_app(initialise_pool=False)
        conn = _Conn(payload)
        app.dependency_overrides[get_db_conn] = lambda: conn
        return TestClient(app), conn

    return _make


URL = "/tiles/get_tile/16/11748/26478"


class TestAllowlist:
    """The function name is an allowlist, never a parameter.

    ⚠ A route that takes a function name out of a URL and calls it is an RPC
    endpoint into Postgres, which has plenty of `bytea`-returning functions
    and some of them are `lo_get`. The repository already refused this shape
    once, in `cog_security.validated_cog_path`, because titiler's default
    would hand any `url` to GDAL and serve `file:///etc/passwd`.
    """

    @pytest.mark.parametrize("function", sorted(TILE_FUNCTIONS))
    def test_every_registered_function_is_reachable(self, served, function) -> None:
        client, _ = served(SMALL_TILE)
        assert client.get(f"/tiles/{function}/16/11748/26478").status_code == 200

    @pytest.mark.parametrize(
        "function",
        [
            "lo_get",  # a real bytea-returning function in every database
            "pg_read_binary_file",
            "get_tile; DROP TABLE curated_geo.feature",
            "feature_current_attributes_mv",  # a real relation, not a tile function
            "",
        ],
    )
    def test_anything_else_is_404_and_touches_no_connection(
        self, served, function
    ) -> None:
        client, conn = served(BIG_TILE)
        response = client.get(f"/tiles/{function}/16/11748/26478")
        assert response.status_code == 404
        assert conn.statements == [], (
            "an unknown function reached the database before being rejected"
        )

    def test_the_rejection_names_what_is_available(self, served) -> None:
        """The set is public — these are world-readable layers — and naming it
        is more useful to a client than a bare 404."""
        client, _ = served(BIG_TILE)
        detail = client.get("/tiles/nope/1/0/0").json()["detail"]
        assert "get_tile" in detail

    def test_the_name_is_passed_as_an_identifier_not_interpolated(self, served) -> None:
        """The allowlist is the security boundary; this is the second gate.

        `sql.Identifier` is what keeps a future mistake in the registry from
        becoming an injection rather than a 404.
        """
        client, conn = served(SMALL_TILE)
        client.get("/tiles/indicator_overlay_tile/16/11748/26478")
        statement, _ = conn.tile_query
        identifiers = [
            part for part in statement.seq if isinstance(part, sql.Identifier)
        ]
        assert [part.strings for part in identifiers] == [
            ("serving_geo", "indicator_overlay_tile")
        ], "the function name was not passed as a quoted identifier"


class TestCoordinateBounds:
    """Rejected before the database, which is both correct and cheap.

    `ST_TileEnvelope` accepts nonsense cheerfully and the query then scans
    for features that cannot be there, so a client walking `z=30` turns a
    typo into database load.
    """

    @pytest.mark.parametrize(
        ("z", "x", "y"),
        [
            (0, 0, 0),
            (1, 1, 1),
            (16, 11748, 26478),
            (24, (1 << 24) - 1, (1 << 24) - 1),
        ],
    )
    def test_addressable_tiles_are_served(self, served, z, x, y) -> None:
        client, _ = served(SMALL_TILE)
        assert client.get(f"/tiles/get_tile/{z}/{x}/{y}").status_code == 200

    @pytest.mark.parametrize(
        ("z", "x", "y"),
        [
            (25, 0, 0),  # past the grid a 32-bit index can address
            (30, 1, 1),
            (-1, 0, 0),
            (2, 4, 0),  # x == 2^z, one past the last column
            (2, 0, 4),
            (2, -1, 0),
            (0, 1, 0),  # z0 has exactly one tile
        ],
    )
    def test_unaddressable_tiles_are_422_and_touch_no_connection(
        self, served, z, x, y
    ) -> None:
        client, conn = served(BIG_TILE)
        assert client.get(f"/tiles/get_tile/{z}/{x}/{y}").status_code == 422
        assert conn.statements == []


class TestEmptyTile:
    """⚠ Empty is zero-length, not NULL, and the answer is 204 not 404.

    `ST_AsMVT` over an empty set returns zero-length bytes — verified against
    the database, `serving_geo.get_tile(14,0,0)` gives `length = 0,
    IS NULL = false` — so a route testing for NULL would answer 200 with an
    empty body forever, and a client would cache an empty tile as content.

    204 rather than 404 because the tile genuinely exists and is empty:
    ocean, or outside the loaded territory, which is most of the world. 404
    would make a map log an error for every tile beyond the data.
    """

    @pytest.mark.parametrize("payload", [b"", None])
    def test_nothing_to_draw_is_204(self, served, payload) -> None:
        client, _ = served(payload)
        response = client.get(URL)
        assert response.status_code == 204
        assert response.content == b""

    def test_an_empty_tile_is_still_cacheable(self, served) -> None:
        """Otherwise a map re-requests every empty tile on every pan."""
        client, _ = served(b"")
        assert "max-age" in client.get(URL).headers["cache-control"]


class TestConditionalRequests:
    """The highest-value optimisation on this path: a 304 buys 100%."""

    def test_a_matching_validator_yields_304_with_no_body(self, served) -> None:
        client, _ = served(BIG_TILE)
        first = client.get(URL, headers={"Accept-Encoding": "identity"})
        etag = first.headers["ETag"]
        again = client.get(
            URL, headers={"Accept-Encoding": "identity", "If-None-Match": etag}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["ETag"] == etag

    def test_a_stale_validator_yields_the_tile(self, served) -> None:
        client, _ = served(BIG_TILE)
        response = client.get(URL, headers={"If-None-Match": '"not-this-tile"'})
        assert response.status_code == 200

    def test_a_list_of_validators_is_honoured(self, served) -> None:
        """`If-None-Match` is a comma-separated list, per RFC 9110."""
        client, _ = served(BIG_TILE)
        etag = client.get(URL, headers={"Accept-Encoding": "identity"}).headers["ETag"]
        response = client.get(
            URL,
            headers={
                "Accept-Encoding": "identity",
                "If-None-Match": f'"other", {etag}, "another"',
            },
        )
        assert response.status_code == 304

    def test_different_tiles_get_different_validators(self, served) -> None:
        a, _ = served(BIG_TILE)
        b, _ = served(BIG_TILE + b"different")
        assert (
            a.get(URL, headers={"Accept-Encoding": "identity"}).headers["ETag"]
            != b.get(URL, headers={"Accept-Encoding": "identity"}).headers["ETag"]
        )


class TestEncoding:
    """⚠ The validator is encoding-specific, and that is not fussiness.

    A shared cache that stored a gzip body under an identity tag would later
    serve those bytes to a client that did not ask for gzip: an unreadable
    tile, and a bug nobody can reproduce locally. The paired tag plus
    `Vary: Accept-Encoding` is what makes it impossible.
    """

    def test_gzip_is_used_when_offered(self, served) -> None:
        client, _ = served(BIG_TILE)
        response = client.get(URL, headers={"Accept-Encoding": "gzip"})
        assert response.headers["Content-Encoding"] == "gzip"

    def test_identity_is_served_verbatim(self, served) -> None:
        client, _ = served(BIG_TILE)
        response = client.get(URL, headers={"Accept-Encoding": "identity"})
        assert "Content-Encoding" not in response.headers
        assert response.content == BIG_TILE

    def test_the_two_encodings_carry_different_validators(self, served) -> None:
        client, _ = served(BIG_TILE)
        identity = client.get(URL, headers={"Accept-Encoding": "identity"})
        gzipped = client.get(URL, headers={"Accept-Encoding": "gzip"})
        assert identity.headers["ETag"] != gzipped.headers["ETag"]
        # Same content hash, different encoding suffix. Compared on the
        # unquoted value: `"abc"` is not a substring of `"abc-gzip"`, because
        # of the closing quote.
        digest = identity.headers["ETag"].strip('"')
        assert gzipped.headers["ETag"].strip('"') == f"{digest}-gzip"

    def test_a_gzip_validator_does_not_satisfy_an_identity_request(
        self, served
    ) -> None:
        """The failure the pairing exists to prevent, asserted directly."""
        client, _ = served(BIG_TILE)
        gzip_etag = client.get(URL, headers={"Accept-Encoding": "gzip"}).headers["ETag"]
        response = client.get(
            URL,
            headers={"Accept-Encoding": "identity", "If-None-Match": gzip_etag},
        )
        assert response.status_code == 200, (
            "an identity request was told its cached gzip body is current, so "
            "a shared cache may hand compressed bytes to a client that cannot "
            "decode them"
        )

    def test_vary_names_accept_encoding(self, served) -> None:
        client, _ = served(BIG_TILE)
        # CORS middleware appends `Origin`; membership, not equality.
        assert "Accept-Encoding" in client.get(URL).headers["Vary"]

    def test_a_small_tile_is_not_compressed(self, served) -> None:
        """Below the threshold gzip's own 18-byte frame costs more than it saves."""
        client, _ = served(SMALL_TILE)
        response = client.get(URL, headers={"Accept-Encoding": "gzip"})
        assert "Content-Encoding" not in response.headers
        assert len(SMALL_TILE) < GZIP_MIN_BYTES

    def test_the_compressed_body_round_trips(self, served) -> None:
        """Proves the bytes are a real gzip member, not a mislabelled body.

        httpx decompresses transparently, so this reads the raw stream.
        """
        client, _ = served(BIG_TILE)
        with client.stream("GET", URL, headers={"Accept-Encoding": "gzip"}) as response:
            raw = b"".join(response.iter_raw())
        assert gzip.decompress(raw) == BIG_TILE
        assert len(raw) < len(BIG_TILE)


class TestResponseShape:
    def test_the_media_type_is_the_registered_one(self, served) -> None:
        """Shared caches and CDNs route on it; octet-stream works locally and
        is diagnosed a week later."""
        client, _ = served(SMALL_TILE)
        assert client.get(URL).headers["content-type"] == MVT_MEDIA_TYPE

    def test_the_query_string_is_forwarded_as_json(self, served) -> None:
        """Wholesale, exactly as Martin does: the SQL functions decide which
        keys mean anything, so filtering here would couple this module to
        every migration that teaches a function a new parameter."""
        client, conn = served(SMALL_TILE)
        client.get(f"{URL}?type_slug=block&role=analysis_unit")
        _, params = conn.tile_query
        assert params[3] == '{"type_slug": "block", "role": "analysis_unit"}'

    def test_no_query_string_forwards_an_empty_object(self, served) -> None:
        client, conn = served(SMALL_TILE)
        client.get(URL)
        _, params = conn.tile_query
        assert params[3] == "{}"

    def test_the_coordinate_reaches_the_function_in_order(self, served) -> None:
        client, conn = served(SMALL_TILE)
        client.get("/tiles/get_tile/16/11748/26478")
        _, params = conn.tile_query
        assert params[:3] == (16, 11748, 26478)

    def test_the_query_runs_read_only_and_time_boxed(self, served) -> None:
        """`STABLE` functions, so read-only is free and removes a category of
        accident. `SET LOCAL` so neither setting outlives the read."""
        client, conn = served(SMALL_TILE)
        client.get(URL)
        issued = conn.plain_sql
        assert "statement_timeout" in issued
        assert "transaction_read_only" in issued
        assert issued.count("SET LOCAL") == 2, "a session-level SET would leak"

    def test_the_read_is_scoped_to_a_savepoint(self, served) -> None:
        """⚠ The regression test for a real bug in the first version.

        `SET LOCAL` is transaction-scoped, so setting
        `transaction_read_only = on` on the ambient transaction and then
        calling `conn.rollback()` was correct for a request that owns its
        connection and quietly destructive otherwise: later writes in that
        transaction failed, and a caller's uncommitted work was discarded.
        The integration suite, which pins the API to its own seeding
        transaction, hit both on the first run.

        Postgres stacks GUC settings per subtransaction, so a savepoint
        rollback releases the settings and leaves an outer transaction
        untouched.
        """
        client, conn = served(SMALL_TILE)
        client.get(URL)
        issued = conn.plain_sql
        assert "SAVEPOINT reimagina_tile" in issued
        assert "ROLLBACK TO SAVEPOINT reimagina_tile" in issued
        assert "RELEASE SAVEPOINT reimagina_tile" in issued
        assert conn.rollbacks == 0, (
            "the route rolled back the whole transaction, which discards "
            "uncommitted work it did not create"
        )

    def test_a_failing_query_still_releases_the_savepoint(self, served) -> None:
        """In `finally`, so one bad tile cannot leave the transaction
        read-only for everything after it."""
        client, conn = served(SMALL_TILE)

        original = _Cursor.execute

        def _explode(self, statement, params=None):
            original(self, statement, params)
            if isinstance(statement, sql.Composed):
                raise RuntimeError("statement timeout, say")

        _Cursor.execute = _explode
        try:
            client.get(URL)
        except RuntimeError:
            pass  # the route re-raises; the API's error handlers own the 500
        finally:
            _Cursor.execute = original

        assert "ROLLBACK TO SAVEPOINT reimagina_tile" in conn.plain_sql


class TestTileJson:
    """Here so the route is drop-in for the resolver's `tilejson_url` too.

    Martin serves TileJSON at the function's own path. Without this, half the
    tile manifest would point at something that 404s, and the asymmetry would
    surface as a frontend bug rather than a missing endpoint.
    """

    def test_it_advertises_the_tile_template(self, served) -> None:
        client, _ = served(SMALL_TILE)
        body = client.get("/tiles/get_tile").json()
        assert body["tilejson"] == "3.0.0"
        assert body["tiles"] == ["http://testserver/tiles/get_tile/{z}/{x}/{y}"]
        assert body["scheme"] == "xyz"

    def test_the_url_follows_the_request_not_the_configuration(self, served) -> None:
        """So a deployment behind a proxy advertises the URL the client
        actually reached."""
        client, _ = served(SMALL_TILE)
        body = client.get(
            "/tiles/get_tile", headers={"Host": "tiles.example.org"}
        ).json()
        assert body["tiles"] == ["http://tiles.example.org/tiles/get_tile/{z}/{x}/{y}"]

    def test_an_unknown_function_has_no_tilejson(self, served) -> None:
        client, _ = served(SMALL_TILE)
        assert client.get("/tiles/lo_get").status_code == 404

    def test_the_advertised_maxzoom_is_renderable_not_merely_addressable(
        self, served
    ) -> None:
        """22 rather than the validator's 24: advertising a zoom no basemap
        reaches would have clients request tiles that are valid and always
        empty."""
        client, _ = served(SMALL_TILE)
        assert client.get("/tiles/get_tile").json()["maxzoom"] == 22
