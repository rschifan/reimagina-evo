"""Integration tests for the reimagina read-side API.

Every test reuses the shared `conn` fixture from tests/conftest.py (the
rollback-on-teardown Postgres connection), overrides FastAPI's database
dependency so route handlers see the same in-flight transaction, seeds the
catalog + a tiny fixture using existing helpers, and asserts the HTTP
response shape.

No real data is committed; every test rolls back cleanly.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn

from reimagina.data import aoi, feature as feat
from reimagina.indicators.catalog import IndicatorSpec, seed_catalog
from reimagina.indicators.compactness import compute_block_compactness


@pytest.fixture
def client(conn):
    """FastAPI TestClient wired to reuse the per-test connection.

    The API dep normally checks a connection out of the process-wide
    pool; here we pin it to the transaction the test is already using so
    seed data is visible to route handlers and nothing committed leaks to
    the DB. `initialise_pool=False` skips creating a real pool since
    every `get_db_conn` call is intercepted by the override.
    """
    app = create_app(initialise_pool=False)

    def _override_conn():
        yield conn

    app.dependency_overrides[get_db_conn] = _override_conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seeded_block_with_compactness(conn):
    """Seed catalog + one MEX:block with a computed compactness indicator.

    Returns a dict with feature_id (+ slug) so tests can cross-check values.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="api_route_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="api-blk-1",
        geometry_wkt="POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,-99.140 19.433,-99.140 19.430))",
    )
    compute_block_compactness(conn, code_version="test")
    return {"block_id": block_id, "slug": "urban_form_block_compactness"}


def test_liveness_always_returns_ok(client):
    """Liveness must not touch the DB and must always be 200."""
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_db_reachable(client):
    """Readiness probes the DB; reachable case."""
    response = client.get("/health/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db"] == "reachable"


def test_health_alias_matches_readiness_shape(client):
    """Legacy /health alias keeps the pre-split response shape."""
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"status", "db"}


def test_tile_server_health_surfaces_db_drift_signal(client, monkeypatch):
    """The /health/tile-server endpoint must (a) report Martin
    reachability, (b) print the API's resolved Postgres database
    name, and (c) include an operator-facing note pointing at the
    common API↔Martin DB-drift bug. We mock Martin to be reachable
    here; the failure path is asserted in the next test."""
    import urllib.request as _urlreq

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(_urlreq, "urlopen", lambda *_, **__: _FakeResponse())
    monkeypatch.setenv("REIMAGINA_MARTIN_URL", "http://localhost:3000")

    response = client.get("/health/tile-server")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["martin_reachable"] is True
    # The API's database name must be reported so operators can
    # compare against Martin's DSN by eyeball.
    assert payload["api_database"] is not None
    # The note must mention the cross-check the operator should run.
    notes_text = " ".join(payload["notes"])
    assert "Martin" in notes_text
    assert "database" in notes_text


def test_tile_server_health_returns_503_when_martin_down(client, monkeypatch):
    """Unreachable Martin → 503 + status=degraded; the operator's
    load balancer can pull this pod out of rotation when the tile
    layer is broken even if the API itself is still serving JSON."""
    import urllib.error
    import urllib.request as _urlreq

    def _raise(*_, **__):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(_urlreq, "urlopen", _raise)
    monkeypatch.setenv("REIMAGINA_MARTIN_URL", "http://localhost:3000")

    response = client.get("/health/tile-server")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["martin_reachable"] is False


def test_every_response_carries_request_id(client):
    response = client.get("/health/live")
    assert "x-request-id" in {k.lower() for k in response.headers}


def test_incoming_request_id_is_echoed_back(client):
    response = client.get(
        "/health/live",
        headers={"X-Request-Id": "test-trace-123"},
    )
    assert response.headers["X-Request-Id"] == "test-trace-123"


def test_list_indicators_returns_seeded_catalog(client, conn):
    seed_catalog(conn)
    response = client.get("/indicators")
    assert response.status_code == 200
    payload = response.json()
    slugs = {row["slug"] for row in payload}
    assert "urban_form_block_compactness" in slugs
    assert "urban_form_block_building_density" in slugs

    # Numeric bounds and aggregation method make it back through the schema.
    compactness = next(
        r for r in payload if r["slug"] == "urban_form_block_compactness"
    )
    assert compactness["unit"] == "ratio_0_1"
    assert compactness["valid_max"] == 1.0
    assert compactness["aggregation_method"] == "weighted_mean"
    assert "block" in compactness["applies_to"]


def test_get_indicator_404s_on_unknown_slug(client):
    response = client.get("/indicators/not_a_real_indicator")
    assert response.status_code == 404


def test_list_values_returns_measured_row(client, seeded_block_with_compactness):
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values?limit=10"
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    row = payload[0]
    assert row["feature_id"] == seeded_block_with_compactness["block_id"]
    assert row["indicator_slug"] == "urban_form_block_compactness"
    assert row["feature_type"] == "block"
    assert row["quality_flag"] == "measured"
    assert 0.0 <= row["value_numeric"] <= 1.0
    assert row["unit"] == "ratio_0_1"


def test_list_values_404s_on_unknown_slug(client, conn):
    seed_catalog(conn)
    response = client.get("/indicators/not_a_real_indicator/values")
    assert response.status_code == 404


def test_list_aois_returns_inserted_row(client, conn):
    from tests.integration._helpers import insert_project as _insert_project

    project_id = _insert_project(conn, name="API test project")
    aoi.create_aoi(
        conn,
        project_id=project_id,
        name="API test AOI",
        geometry_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    )

    response = client.get("/aois")
    assert response.status_code == 200
    payload = response.json()
    assert any(row["name"] == "API test AOI" for row in payload)


def test_aoi_summaries_404s_on_unknown_aoi(client):
    response = client.get("/aois/00000000-0000-0000-0000-000000000000/summaries")
    assert response.status_code == 404


def test_features_endpoint_returns_paginated_rows(client, conn):
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )

    source_id = _register_source(
        conn,
        name="api_features_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    for i in range(3):
        _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"pg-{i}",
            geometry_wkt=f"POLYGON(({i} {i}, {i + 1} {i}, {i + 1} {i + 1}, {i} {i + 1}, {i} {i}))",
        )

    response = client.get("/features?type_slug=block&limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["features"]) == 2
    # Every row back is a block.
    assert all(f["type_slug"] == "block" for f in payload["features"])


@pytest.fixture
def seeded_many_blocks(conn):
    """Several blocks with a spread of compactness values.

    The one-feature fixture cannot exercise a histogram: with a single value
    `min == max`, which is the degenerate single-bin path rather than the
    normal one. These blocks differ in shape, so compactness differs too, and
    the distribution has a real low end and a real high end.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="hist_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    # Rectangles of increasing elongation. A square is maximally compact for
    # a rectangle; a sliver is minimally so — so this spans the measure's
    # range rather than clustering, and the bins have something to separate.
    for index in range(8):
        west = -99.150 + index * 0.004
        height = 0.003
        width = 0.003 / (index + 1)
        _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"hist-blk-{index}",
            geometry_wkt=(
                f"POLYGON(({west} 19.430,{west + width} 19.430,"
                f"{west + width} {19.430 + height},{west} {19.430 + height},"
                f"{west} 19.430))"
            ),
        )
    compute_block_compactness(conn, code_version="test")
    return {"slug": "urban_form_block_compactness", "blocks": 8}


def test_stats_endpoint_returns_percentiles(client, seeded_block_with_compactness):
    response = client.get(f"/indicators/{seeded_block_with_compactness['slug']}/stats")
    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == seeded_block_with_compactness["slug"]
    assert payload["count"] == 1
    # With one row every quantile equals the value, so min == p50 == max.
    for key in ("min", "p05", "p50", "p95", "max"):
        assert payload[key] is not None
    assert payload["min"] == payload["max"]


def test_stats_endpoint_returns_empty_for_unseeded_indicator(client, conn):
    seed_catalog(conn)
    response = client.get("/indicators/urban_form_block_building_density/stats")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    for key in ("min", "p05", "p50", "p95", "max"):
        assert payload[key] is None


class TestStatsHistogram:
    """Distribution shape, which min/max/median cannot carry.

    Two territories with identical summary statistics can have completely
    different distributions, so a legend built from percentiles alone renders
    a pattern without letting anyone place a value inside it.

    ⚠ Server-side by necessity, not preference. The obvious client
    implementation is `queryRenderedFeatures`, and `selection-metrics`
    already abandoned exactly that because it "silently undercounted whenever
    the renderer hadn't loaded the relevant tiles". A histogram that changes
    shape while tiles load is worse than none, because a reader cannot tell
    which shape was the real one.
    """

    def test_bins_are_absent_unless_asked_for(
        self, client, seeded_block_with_compactness
    ) -> None:
        """The default response is byte-for-byte what it always was."""
        slug = seeded_block_with_compactness["slug"]
        assert client.get(f"/indicators/{slug}/stats").json()["histogram"] is None

    def test_the_bins_account_for_every_counted_value(
        self, client, seeded_many_blocks
    ) -> None:
        """The histogram must describe the same population as `count`.

        A bin array that sums to less than `count` means values fell outside
        every bin, which is how a distribution quietly loses its own
        extremes.
        """
        response = client.get(f"/indicators/{seeded_many_blocks['slug']}/stats?bins=8")
        payload = response.json()
        histogram = payload["histogram"]
        assert histogram["bins"] == 8
        assert len(histogram["counts"]) == 8
        assert sum(histogram["counts"]) == payload["count"]

    def test_the_maximum_lands_inside_the_histogram(
        self, client, seeded_many_blocks
    ) -> None:
        """⚠ `width_bucket` puts a value equal to `hi` in bucket n+1.

        That bucket is an overflow, not a bin, so without folding it back the
        tallest-value bar is always missing exactly the maximum — the single
        value a reader is most likely to go looking for.
        """
        payload = client.get(
            f"/indicators/{seeded_many_blocks['slug']}/stats?bins=4"
        ).json()
        assert payload["histogram"]["counts"][-1] >= 1
        assert payload["histogram"]["hi"] == payload["max"]

    def test_the_range_is_the_full_one_not_the_ramp(
        self, client, seeded_many_blocks
    ) -> None:
        """The ramp clips to p05–p95; the histogram must not.

        Clipping both would hide the outlier the histogram exists to show.
        """
        payload = client.get(
            f"/indicators/{seeded_many_blocks['slug']}/stats?bins=8"
        ).json()
        assert payload["histogram"]["lo"] == payload["min"]
        assert payload["histogram"]["hi"] == payload["max"]

    def test_one_value_is_one_full_bin_rather_than_an_error(
        self, client, seeded_block_with_compactness
    ) -> None:
        """`width_bucket` requires `lo < hi` and raises otherwise.

        One feature is the common case for a fresh AOI, not an edge case.
        """
        payload = client.get(
            f"/indicators/{seeded_block_with_compactness['slug']}/stats?bins=10"
        ).json()
        assert payload["count"] == 1
        assert payload["histogram"]["counts"] == [1]
        assert payload["histogram"]["width"] == 0

    def test_nothing_to_bin_is_null_rather_than_empty_bins(self, client, conn) -> None:
        """An array of zeros would draw a histogram of a population of none."""
        seed_catalog(conn)
        payload = client.get(
            "/indicators/urban_form_block_building_density/stats?bins=10"
        ).json()
        assert payload["count"] == 0
        assert payload["histogram"] is None

    def test_bins_are_scoped_by_aoi_like_every_other_number(
        self, client, two_aois_one_block_each
    ) -> None:
        """Otherwise the legend's shape and its percentiles describe
        different populations."""
        first = two_aois_one_block_each["aoi_a"]
        payload = client.get(
            f"/indicators/{two_aois_one_block_each['slug']}/stats?aoi_id={first}&bins=6"
        ).json()
        assert payload["count"] == 1
        assert sum(payload["histogram"]["counts"]) == payload["count"]

    def test_an_absurd_bin_count_is_rejected_not_served(
        self, client, seeded_block_with_compactness
    ) -> None:
        """The bin count sizes an array the server allocates per request."""
        slug = seeded_block_with_compactness["slug"]
        assert client.get(f"/indicators/{slug}/stats?bins=100000").status_code == 422
        assert client.get(f"/indicators/{slug}/stats?bins=-1").status_code == 422


def test_stats_endpoint_404s_on_unknown_slug(client, conn):
    seed_catalog(conn)
    response = client.get("/indicators/does_not_exist/stats")
    assert response.status_code == 404


def test_values_csv_download_is_streaming_and_has_pinned_header(
    client, seeded_block_with_compactness
):
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values.csv"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    lines = response.text.splitlines()
    assert lines[0] == "feature_id,type_slug,value,quality_flag,computed_at"
    assert len(lines) == 2  # one seeded row + header
    parts = lines[1].split(",")
    assert parts[0] == seeded_block_with_compactness["block_id"]
    assert parts[1] == "block"
    assert float(parts[2]) > 0
    assert parts[3] == "measured"


def test_values_geojson_download_is_valid_featurecollection(
    client, seeded_block_with_compactness
):
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values.geojson"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1
    feature = payload["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert (
        feature["properties"]["feature_id"] == seeded_block_with_compactness["block_id"]
    )
    assert feature["properties"]["type_slug"] == "block"


def test_values_parquet_download_round_trips_through_geopandas(
    client, seeded_block_with_compactness
):
    import io as _io

    import geopandas as gpd

    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values.parquet"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apache.parquet")
    gdf = gpd.read_parquet(_io.BytesIO(response.content))
    assert len(gdf) == 1
    assert gdf.iloc[0]["feature_id"] == seeded_block_with_compactness["block_id"]
    assert gdf.crs is not None
    assert gdf.crs.to_epsg() == 4326


def test_values_downloads_404_on_unknown_slug(client, conn):
    seed_catalog(conn)  # ensure catalog exists, but the specific slug does not
    for ext in ("csv", "geojson", "parquet"):
        response = client.get(f"/indicators/does_not_exist/values.{ext}")
        assert response.status_code == 404, ext


def test_list_values_rejects_negative_limit_with_422(client, conn):
    seed_catalog(conn)
    response = client.get("/indicators/urban_form_block_compactness/values?limit=-1")
    assert response.status_code == 422


def test_list_values_rejects_huge_limit_with_422(client, conn):
    seed_catalog(conn)
    response = client.get(
        "/indicators/urban_form_block_compactness/values?limit=999999999"
    )
    assert response.status_code == 422


def test_features_endpoint_rejects_zero_limit_with_422(client):
    response = client.get("/features?limit=0")
    assert response.status_code == 422


def test_features_endpoint_rejects_malformed_cursor_with_422(client):
    """A bad after_cursor is a client mistake, not a 500."""
    response = client.get("/features?after_cursor=not-a-valid-cursor")
    assert response.status_code == 422


def test_aoi_summaries_rejects_malformed_uuid_with_422(client):
    """UUID-typed path parameter → 422 before any DB round trip."""
    response = client.get("/aois/not-a-uuid/summaries")
    assert response.status_code == 422
    body = response.json()
    # FastAPI uses the standard 'detail' key for validation errors.
    assert "detail" in body


def test_csv_download_rejects_zero_limit_with_422(
    client, seeded_block_with_compactness
):
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values.csv?limit=0"
    )
    assert response.status_code == 422


def test_csv_download_respects_limit_override(client, seeded_block_with_compactness):
    # Only one row exists; limit=1 still returns it.
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/values.csv?limit=1"
    )
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert len(lines) == 2  # header + 1 row


def test_tile_sources_returns_manifest(client):
    response = client.get("/tile-sources")
    assert response.status_code == 200
    payload = response.json()
    layer_ids = {row["layer_id"] for row in payload}
    # Shipped layers from infra/tiles/layers.json.
    assert "baseline_live" in layer_ids
    assert "admin_reference" in layer_ids
    # Each row must have a source_config the frontend can consume.
    for row in payload:
        assert "source_mode" in row
        assert "source_config" in row


# ─── /indicators/{slug}/stats?aoi_id=… ────────────────────────────────


@pytest.fixture
def two_aois_one_block_each(conn):
    """Seed catalog + two blocks, each in a different AOI, with an
    indicator value computed for each. Returns a dict the AOI-stats
    tests use to assert per-AOI scoping.

    AOI A holds the existing 'in-bbox' block with a real compactness
    value. AOI B holds a different block with its own compactness
    value, so the two AOIs have non-overlapping populations and the
    stats endpoint must report different counts.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        insert_project as _insert_project,
        register_source as _register_source,
        start_run as _start_run,
    )

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="aoi_stats_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    block_a = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="aoi-a-blk",
        geometry_wkt="POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,-99.140 19.433,-99.140 19.430))",
    )
    block_b = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="aoi-b-blk",
        geometry_wkt="POLYGON((-115.470 32.640,-115.467 32.640,-115.467 32.643,-115.470 32.643,-115.470 32.640))",
    )
    compute_block_compactness(conn, code_version="test")

    project_a = _insert_project(conn, name="proj-aoi-a")
    project_b = _insert_project(conn, name="proj-aoi-b")
    aoi_a = aoi.create_aoi(
        conn,
        project_id=project_a,
        name="AOI A (CDMX)",
        geometry_wkt="POLYGON((-99.20 19.40,-99.10 19.40,-99.10 19.50,-99.20 19.50,-99.20 19.40))",
    )
    aoi_b = aoi.create_aoi(
        conn,
        project_id=project_b,
        name="AOI B (Mexicali)",
        geometry_wkt="POLYGON((-115.50 32.60,-115.40 32.60,-115.40 32.70,-115.50 32.70,-115.50 32.60))",
    )
    # Direct insert into aoi_feature so the test asserts the API's filter
    # behaviour without depending on compute_overlaps' geometry maths
    # for tiny test geometries.
    with conn.cursor() as cur:
        for aoi_id, feat_id in ((aoi_a, block_a), (aoi_b, block_b)):
            cur.execute(
                """
                INSERT INTO curated_geo.aoi_feature
                  (aoi_id, feature_id, overlap_basis,
                   overlap_measure, feature_measure, overlap_ratio)
                VALUES (%s, %s, 'area', 1.0, 1.0, 1.0)
                """,
                (aoi_id, feat_id),
            )
    return {
        "slug": "urban_form_block_compactness",
        "aoi_a": aoi_a,
        "block_a": block_a,
        "aoi_b": aoi_b,
        "block_b": block_b,
    }


def test_stats_endpoint_includes_aoi_id_in_response(client, two_aois_one_block_each):
    """The response model echoes the requested aoi_id so clients can
    correlate the percentiles with the AOI they're rendering."""
    fixture = two_aois_one_block_each
    response = client.get(
        f"/indicators/{fixture['slug']}/stats?aoi_id={fixture['aoi_a']}"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["aoi_id"] == fixture["aoi_a"]


def test_stats_endpoint_scopes_count_to_picked_aoi(client, two_aois_one_block_each):
    """With two blocks in two AOIs, ?aoi_id=A returns count=1 and
    ?aoi_id=B returns count=1 — and they're different stats slices,
    not a Mexico-wide aggregate."""
    fixture = two_aois_one_block_each
    resp_all = client.get(f"/indicators/{fixture['slug']}/stats")
    resp_a = client.get(
        f"/indicators/{fixture['slug']}/stats?aoi_id={fixture['aoi_a']}"
    )
    resp_b = client.get(
        f"/indicators/{fixture['slug']}/stats?aoi_id={fixture['aoi_b']}"
    )

    assert resp_all.status_code == resp_a.status_code == resp_b.status_code == 200
    assert resp_all.json()["count"] == 2
    assert resp_a.json()["count"] == 1
    assert resp_b.json()["count"] == 1


def test_list_aois_returns_bbox_envelope(client, two_aois_one_block_each):
    """The /aois endpoint exposes ST_Envelope-derived bounds so the web
    picker can fly the map without a separate geometry round-trip."""
    fixture = two_aois_one_block_each
    response = client.get("/aois")
    assert response.status_code == 200
    payload = response.json()
    by_id = {row["aoi_id"]: row for row in payload}

    assert fixture["aoi_a"] in by_id
    assert fixture["aoi_b"] in by_id
    bbox_a = by_id[fixture["aoi_a"]]["bbox"]
    bbox_b = by_id[fixture["aoi_b"]]["bbox"]

    # AOI A's polygon spans (-99.20..-99.10, 19.40..19.50); the bbox must
    # match those four corners (within FP epsilon) and stay in lon/lat
    # order for the web client's fitBounds([sw, ne]) to make sense.
    assert bbox_a == pytest.approx([-99.20, 19.40, -99.10, 19.50])
    # AOI B is in Mexicali (~115.4 W) — distinctly different bbox so the
    # picker actually moves the map between selections.
    assert bbox_b[0] < -115.0
    assert bbox_b[2] < -115.0


def test_stats_endpoint_with_unknown_aoi_id_is_empty(client, two_aois_one_block_each):
    """A junk AOI uuid returns count=0 with all percentile fields None.
    That's the expected shape — same as 'no rows for this slug yet' —
    so frontends can fall back to catalog bounds without special-casing."""
    fixture = two_aois_one_block_each
    bogus = "00000000-0000-0000-0000-000000000001"
    response = client.get(f"/indicators/{fixture['slug']}/stats?aoi_id={bogus}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    for key in ("min", "p05", "p50", "p95", "max"):
        assert payload[key] is None
    assert payload["aoi_id"] == bogus


# ── POST /jobs — the submit endpoint's two response modes ───────────────────
#
# Isolated wiring tests: the enqueue/drain internals are covered deeply in the
# orchestration suites, so here we swap the whole orchestrator out and only
# assert the router wires the modes correctly (default 202+queued vs wait
# 200+finished). ``connect`` is stubbed so no real DB/queue is touched.
#
# Both are faked at the same seam — ``factory.get_orchestrator``. The route no
# longer names a mode at all: with one ops-plane, and one that a single process
# can both submit to and drain, ``wait`` chooses a response shape rather than an
# engine.


class _StubConn:
    """Enough connection to satisfy the route: it polls, so it ends read txns.

    ⚠ Shaped like a *connection*, including ``close``, because that is what
    ``reimagina.db.connect`` returns. The stub used to be a context manager
    yielding one, which no route could have closed even if it tried.
    """

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass


def _stub_connect(monkeypatch):
    monkeypatch.setattr("reimagina.db.connect", lambda dsn=None: _StubConn())


def _install(monkeypatch, handle, *, final=None):
    """Swap the whole orchestrator out; the route names no engine any more.

    ``final`` is what ``get_run`` reports, for the ``wait: true`` path — which
    now polls, because submitting to a durable-execution library returns as soon
    as the run is durable rather than when it is done.
    """

    class _Fake:
        def submit_run(self, conn, **kw):
            return handle

        def get_run(self, conn, run_id):
            return final

    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator", lambda mode=None: _Fake()
    )


def test_submit_run_default_is_fire_and_forget_202(client, monkeypatch):
    from reimagina.orchestration.port import RunHandle

    _stub_connect(monkeypatch)
    _install(monkeypatch, RunHandle(run_id="r-queued", status=None))

    response = client.post(
        "/jobs",
        json={"changed_attributes": ["geometry"], "edited_features": ["f1"]},
    )

    assert response.status_code == 202
    # `deduplicated` joined the wire contract when submit learned to collapse
    # a resubmission onto work already in flight. False here: this is a
    # fresh submit, and the flag exists so a client can tell the difference.
    assert response.json() == {
        "run_id": "r-queued",
        "status": "queued",
        "deduplicated": False,
    }


def test_submit_run_wait_drains_and_returns_200(client, monkeypatch):
    from reimagina.orchestration.port import RunHandle, RunStatus, UnitStatusView

    _stub_connect(monkeypatch)
    _install(
        monkeypatch,
        RunHandle(run_id="r-done", status=None),
        final=RunStatus(
            run_id="r-done",
            status="succeeded",
            units=(UnitStatusView("urban_form_block_compactness", "succeeded", None),),
        ),
    )

    response = client.post(
        "/jobs",
        json={
            "wait": True,
            "changed_attributes": ["geometry"],
            "edited_features": ["f1"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "r-done",
        "status": "succeeded",
        "deduplicated": False,
    }


def test_list_indicators_aoi_filter_hides_uncomputed(client, conn):
    """``GET /indicators?aoi_id=`` offers only indicators with values there.

    The catalog records what an indicator *is*; it says nothing about whether
    anyone has computed it. ``urban_form_block_planned_floors`` sat registered
    with zero rows until the ``floors`` attribute was seeded, and the picker
    offered it — selecting it painted an empty map, which reads as a broken
    indicator rather than an absent one.
    """
    from tests.integration.test_tile_aoi_filter import (
        _attach_feature_to_aoi,
        _insert_indicator_result,
        _make_aoi,
        _polygon_at,
    )
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )
    from reimagina.data import feature as feat

    src = _register_source(
        conn,
        name=f"picker_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run = _start_run(conn, src)
    feature_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=src,
        ingestion_run_id=run,
        dataset_version="v1",
        source_record_ref=f"picker-{uuid4().hex[:8]}",
        geometry_wkt=_polygon_at(-99.135, 19.430),
    )
    aoi_id = _make_aoi(
        conn,
        project_name=f"proj-picker-{uuid4().hex[:6]}",
        aoi_name="AOI picker",
        polygon_wkt=_polygon_at(-99.135, 19.430, side=0.05),
    )
    _attach_feature_to_aoi(conn, aoi_id=aoi_id, feature_id=feature_id)

    computed = f"picker_computed_{uuid4().hex[:8]}"
    _insert_indicator_result(
        conn,
        slug=computed,
        feature_id=feature_id,
        feature_type="block",
        value_numeric=1.5,
    )
    # Registered in the catalog, never computed anywhere.
    uncomputed = f"picker_uncomputed_{uuid4().hex[:8]}"
    seed_catalog(
        conn,
        (
            IndicatorSpec(
                slug=uncomputed,
                name="Uncomputed",
                computation_kind="direct",
                model_family="urban_form",
                value_type="numeric",
                aggregation_method="mean",
                applies_to=("block",),
            ),
        ),
    )

    unfiltered = {r["slug"] for r in client.get("/indicators").json()}
    assert computed in unfiltered
    assert uncomputed in unfiltered, "the bare catalog listing must not filter"

    scoped = {r["slug"] for r in client.get(f"/indicators?aoi_id={aoi_id}").json()}
    assert computed in scoped
    assert uncomputed not in scoped

    # An AOI nobody has computed anything for offers nothing, rather than
    # offering the whole catalog.
    empty_aoi = _make_aoi(
        conn,
        project_name=f"proj-empty-{uuid4().hex[:6]}",
        aoi_name="AOI empty",
        polygon_wkt=_polygon_at(-100.0, 20.0, side=0.05),
    )
    assert client.get(f"/indicators?aoi_id={empty_aoi}").json() == []


def test_runstore_answers_a_malformed_run_id_instead_of_raising(conn):
    """`get_run` is typed `dict | None`, and a driver error is not either one.

    ``orchestration_run.id`` is ``uuid``, so asking Postgres about
    ``'not-a-uuid'`` does not return "no such run" — it raises
    ``invalid input syntax for type uuid`` and aborts the transaction. Through
    the API that surfaced as a 500 (``database_error_handler`` maps every
    non-connection driver error there) for what is plainly a client typo.

    Asserted against a real connection because the guard only matters against a
    real cast; a stub connection short-circuits on the backend check before the
    id is ever looked at.
    """
    from reimagina.orchestration import runstore

    assert runstore.get_run(conn, "not-a-uuid") is None
    # The transaction is still usable — an aborted one would fail here, which is
    # the other half of what the raise used to cost.
    assert runstore.get_run(conn, str(uuid4())) is None


def test_jobs_route_404s_a_malformed_run_id_end_to_end(client):
    """The same thing through the live app, with the real orchestrator."""
    assert client.get("/jobs/not-a-uuid").status_code == 404
