"""The API-served MVT route against real PostGIS, decoding real tile bytes.

This is the suite that closes the gap the route was built for. Before it, the
live vector tile path was proven at the SQL layer (26 tests over the three
tile functions) and at the decoder layer (`scripts/mvt_inspect.py`), with an
unexecuted process — Martin — in between. Nothing ran a tile over HTTP and
decoded what came out the other end. These do.

**No optional decoder, and therefore no skip.** `test_indicator_overlay_tile`
guards itself on `mapbox_vector_tile`, a dev-only dependency, so on a machine
without it the whole file reports green having asserted nothing. That is the
vacuous-green class this repository has already paid for once. The decoder in
`scripts/mvt_inspect.py` is dependency-free and in-tree, so these tests
decode protobuf without importing anything that might be absent.

See `docs/superpowers/specs/API_SERVED_MVT.md`.
"""

from __future__ import annotations

import gzip
import math
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn
from apps.api.routers.mvt import GZIP_MIN_BYTES
from reimagina.data import feature as feat
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.tiles.functions import TILE_FUNCTIONS

from ._helpers import insert_feature, register_source, start_run

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mvt_inspect import decode  # noqa: E402 — in-tree, dependency-free decoder

# A block in Mexicali, and the z16 tile that contains it. Computed rather
# than hard-coded so the two cannot drift apart.
LON, LAT = -115.4656, 32.6267
BLOCK_WKT = (
    "POLYGON((-115.4660 32.6264,-115.4652 32.6264,"
    "-115.4652 32.6270,-115.4660 32.6270,-115.4660 32.6264))"
)


def _tile_of(lon: float, lat: float, z: int) -> tuple[int, int, int]:
    """The XYZ tile containing a lon/lat at zoom ``z`` (Web Mercator)."""
    x = int((lon + 180.0) / 360.0 * (1 << z))
    lat_rad = math.radians(lat)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * (1 << z)
    )
    return z, x, y


Z, X, Y = _tile_of(LON, LAT, 16)
URL = f"/tiles/get_tile/{Z}/{X}/{Y}"


@pytest.fixture
def seeded_tile(conn):
    """One block with a computed indicator, inside the tile under test."""
    seed_catalog(conn)
    source_id = register_source(
        conn,
        name="mvt_route_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    block_id = insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=start_run(conn, source_id),
        dataset_version="v1",
        source_record_ref="mvt-blk-1",
        geometry_wkt=BLOCK_WKT,
    )
    compute_block_compactness(conn, code_version="test")
    return {"block_id": block_id, "slug": "urban_form_block_compactness"}


#: Enough blocks for the tile to cross the gzip threshold. 30 gets a real
#: tile to ~2 KiB here; the test asserts the threshold rather than trusting
#: this number, so a future encoding change fails loudly instead of silently
#: skipping the compression path.
BLOCKS_IN_DENSE_TILE = 30


@pytest.fixture
def dense_tile(conn):
    """A tile with enough features to exercise compression.

    Laid out as a strip of small blocks inside the same z16 tile as
    ``seeded_tile``, so both fixtures answer at the same URL.
    """
    seed_catalog(conn)
    source_id = register_source(
        conn,
        name="mvt_dense_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    type_id = feat.get_type_id(conn, "MEX", "block")
    step = 0.00025
    for index in range(BLOCKS_IN_DENSE_TILE):
        west = -115.4660 + index * step
        east = west + step * 0.8
        insert_feature(
            conn,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"mvt-dense-{index:03d}",
            geometry_wkt=(
                f"POLYGON(({west} 32.6264,{east} 32.6264,"
                f"{east} 32.6270,{west} 32.6270,{west} 32.6264))"
            ),
        )
    return {"count": BLOCKS_IN_DENSE_TILE}


@pytest.fixture
def client(conn):
    """The API pinned to this test's transaction, so seed data is visible.

    The connection is handed over as-is, with no wrapper, and that is worth
    noting: it is only possible because the route scopes its read-only
    setting and its rollback to a savepoint. The first version of the route
    set `transaction_read_only` on the ambient transaction and called
    `conn.rollback()`, so this fixture's uncommitted seed data vanished and
    every later write in the test failed with `cannot execute INSERT in a
    read-only transaction`. The route is self-contained now; this fixture is
    the check that it stays that way.
    """
    app = create_app(initialise_pool=False)
    app.dependency_overrides[get_db_conn] = lambda: conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestRealTileBytes:
    def test_a_populated_tile_decodes_to_the_seeded_block(
        self, client, seeded_tile
    ) -> None:
        """The end-to-end assertion: HTTP in, decodable MVT out."""
        response = client.get(URL, headers={"Accept-Encoding": "identity"})
        assert response.status_code == 200
        assert response.headers["content-type"] == (
            "application/vnd.mapbox-vector-tile"
        )

        layers = decode(response.content)
        assert [layer["name"] for layer in layers] == ["features"]

        # ⚠ Found by name, not by being the only feature in the tile.
        #
        # An absolute count asserts something this test does not own. Other
        # integration tests commit features, some of them inside this tile,
        # so `len(features) == 1` passed in isolation and failed at 5 in a
        # full run — a test reporting on its neighbours rather than on the
        # route.
        mine = [
            f
            for f in layers[0]["features"]
            if f["properties"].get("id") == seeded_tile["block_id"]
        ]
        assert len(mine) == 1, (
            f"the seeded block is not in the tile; it carries "
            f"{[f['properties'].get('id') for f in layers[0]['features']]}"
        )

        only = mine[0]
        assert only["properties"]["type_slug"] == "block"
        assert only["geometry_type"] == "POLYGON"
        # Geometry commands actually present, not just a tagged empty
        # feature: a tile that encodes properties and no coordinates draws
        # nothing and would otherwise pass every assertion above.
        assert only["geometry_commands"] > 0

    def test_the_extent_is_the_mvt_default(self, client, seeded_tile) -> None:
        """4096 is what `ST_AsMVT` is called with and what MapLibre assumes."""
        layers = decode(
            client.get(URL, headers={"Accept-Encoding": "identity"}).content
        )
        assert layers[0]["extent"] == 4096

    def test_a_tile_outside_the_territory_is_204(self, client, seeded_tile) -> None:
        """⚠ Zero-length, not NULL, which is why this is a status assertion.

        `ST_AsMVT` over an empty set returns empty bytes — checked directly
        against the database: `serving_geo.get_tile(14,0,0)` gives
        `length = 0, IS NULL = false`. A route testing for NULL would answer
        200 with an empty body here, and a client would cache emptiness as
        content.
        """
        response = client.get("/tiles/get_tile/14/0/0")
        assert response.status_code == 204
        assert response.content == b""

    def test_a_query_parameter_reaches_the_sql_function(
        self, client, seeded_tile
    ) -> None:
        """Proves the forwarding, not just that a tile came back."""
        kept = client.get(
            f"{URL}?type_slug=block", headers={"Accept-Encoding": "identity"}
        )
        kept_slugs = {
            f["properties"].get("type_slug")
            for f in decode(kept.content)[0]["features"]
        }
        assert kept_slugs == {"block"}, (
            f"the type_slug filter did not reach the function: {kept_slugs}"
        )
        assert seeded_tile["block_id"] in {
            f["properties"].get("id") for f in decode(kept.content)[0]["features"]
        }

        # A type that exists in the catalog but has nothing in this tile.
        dropped = client.get(
            f"{URL}?type_slug=a_type_slug_nothing_uses",
            headers={"Accept-Encoding": "identity"},
        )
        assert dropped.status_code == 204

    @pytest.mark.parametrize("function", sorted(TILE_FUNCTIONS))
    def test_every_registered_function_is_callable_against_postgis(
        self, client, seeded_tile, function
    ) -> None:
        """The registry and the database must agree.

        A name in `TILE_FUNCTIONS` that `serving_geo` does not have would be
        a 500 on a hot path — and the two live in different places (a Python
        frozenset and a migration), so nothing else would notice them
        drifting.
        """
        response = client.get(f"/tiles/{function}/{Z}/{X}/{Y}")
        assert response.status_code in {200, 204}, response.text

    def test_the_indicator_overlay_carries_its_value(self, client, seeded_tile) -> None:
        response = client.get(
            f"/tiles/indicator_overlay_tile/{Z}/{X}/{Y}"
            f"?indicator_slug={seeded_tile['slug']}&feature_type=block",
            headers={"Accept-Encoding": "identity"},
        )
        assert response.status_code == 200
        features = decode(response.content)[0]["features"]
        assert len(features) == 1
        props = features[0]["properties"]
        assert props["feature_id"] == seeded_tile["block_id"]
        assert props["type_slug"] == "block"
        # Compactness is a ratio, so any real value is in (0, 1].
        assert 0 < props["value_numeric"] <= 1
        assert props["quality_flag"] == "measured"


class TestCachingOverRealBytes:
    def test_the_validator_round_trips_to_304(self, client, seeded_tile) -> None:
        first = client.get(URL, headers={"Accept-Encoding": "identity"})
        etag = first.headers["ETag"]
        again = client.get(
            URL, headers={"Accept-Encoding": "identity", "If-None-Match": etag}
        )
        assert again.status_code == 304
        assert again.content == b""

    def test_the_validator_changes_when_the_tile_does(
        self, client, seeded_tile, conn
    ) -> None:
        """A content-derived ETag, not a timestamp or a counter.

        Adding a feature inside the tile must invalidate it, or a client
        keeps painting a stale tile until `max-age` expires.
        """
        before = client.get(URL, headers={"Accept-Encoding": "identity"})
        source_id = register_source(
            conn,
            name="mvt_route_src_2",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        insert_feature(
            conn,
            type_id=feat.get_type_id(conn, "MEX", "block"),
            source_id=source_id,
            ingestion_run_id=start_run(conn, source_id),
            dataset_version="v1",
            source_record_ref="mvt-blk-2",
            geometry_wkt=(
                "POLYGON((-115.4651 32.6264,-115.4648 32.6264,"
                "-115.4648 32.6270,-115.4651 32.6270,-115.4651 32.6264))"
            ),
        )
        after = client.get(URL, headers={"Accept-Encoding": "identity"})
        # Scoped to this test's own rows — see the note in
        # `test_a_populated_tile_decodes_to_the_seeded_block`.
        names = {
            f["properties"].get("name") for f in decode(after.content)[0]["features"]
        }
        assert {"mvt-blk-1", "mvt-blk-2"} <= names
        assert after.headers["ETag"] != before.headers["ETag"]

    def test_gzip_round_trips_to_the_same_bytes(self, client, dense_tile) -> None:
        """Only meaningful on real tiles: the body has to be a valid gzip
        member *and* decode to the identical protobuf.

        Uses `dense_tile` rather than `seeded_tile` so there is nothing to
        skip. One block encodes to ~156 B, below the compression threshold,
        and a `pytest.skip` here would be the vacuous-green pattern this
        file's docstring objects to: a test that reports success for a code
        path it declined to enter.
        """
        identity = client.get(URL, headers={"Accept-Encoding": "identity"})
        assert len(identity.content) >= GZIP_MIN_BYTES, (
            f"the fixture produced a {len(identity.content)}B tile, which is "
            "below the compression threshold, so this test would prove nothing"
        )
        with client.stream("GET", URL, headers={"Accept-Encoding": "gzip"}) as response:
            raw = b"".join(response.iter_raw())
        assert gzip.decompress(raw) == identity.content
        assert len(raw) < len(identity.content)
        # Decodes after the round trip, not merely byte-equal — and decodes
        # to the same features the identity response carried.
        #
        # Compared against the identity tile rather than against
        # BLOCKS_IN_DENSE_TILE: the strip is wider than one z16 tile
        # (0.0075 deg against ~0.0055 deg), so some blocks land in the
        # neighbour. That is the tile function clipping correctly, and
        # pinning an exact count here would assert the fixture's geometry
        # instead of the compression.
        round_tripped = decode(gzip.decompress(raw))[0]["features"]
        assert len(round_tripped) == len(decode(identity.content)[0]["features"])
        assert len(round_tripped) > 1


class TestTransactionContainment:
    """A tile read must not disturb a transaction it did not open.

    ⚠ This is a regression test for a real bug in the first version of the
    route, which set `transaction_read_only = on` on the ambient transaction
    and then called `conn.rollback()`. On a pooled connection that is
    harmless — the request owns the connection — and anywhere else it is
    quietly destructive: later writes failed, and a caller's uncommitted work
    was discarded.

    `SET LOCAL` is transaction-scoped, so the fix is a savepoint: Postgres
    stacks GUC settings per subtransaction, so `ROLLBACK TO SAVEPOINT` undoes
    the settings and leaves the outer transaction alone.
    """

    def test_a_write_still_works_after_serving_a_tile(
        self, client, seeded_tile, conn
    ) -> None:
        client.get(URL)
        # The failure mode: ReadOnlySqlTransaction raised from here.
        source_id = register_source(
            conn,
            name="mvt_after_tile",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        insert_feature(
            conn,
            type_id=feat.get_type_id(conn, "MEX", "block"),
            source_id=source_id,
            ingestion_run_id=start_run(conn, source_id),
            dataset_version="v1",
            source_record_ref="mvt-after-tile",
            geometry_wkt=BLOCK_WKT,
        )

    def test_seed_data_survives_a_tile_read(self, client, seeded_tile, conn) -> None:
        """The other half: an unscoped `rollback()` discarded it."""
        client.get(URL)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM curated_geo.feature WHERE id = %s",
                (seeded_tile["block_id"],),
            )
            assert cur.fetchone()[0] == 1

    def test_a_failed_tile_read_releases_the_read_only_setting(
        self, client, seeded_tile, conn
    ) -> None:
        """The `finally` on the savepoint rollback, asserted.

        A tile whose SQL raises — here, an overlay called with a malformed
        uuid — must not leave the transaction read-only, or one bad tile
        request would break every write after it.
        """
        client.get(
            f"/tiles/indicator_overlay_tile/{Z}/{X}/{Y}"
            "?indicator_slug=urban_form_block_compactness&aoi_id=not-a-uuid"
        )
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "off"
