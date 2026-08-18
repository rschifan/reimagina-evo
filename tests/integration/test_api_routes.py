"""Integration tests for the reimagina read-side API.

Every test reuses the shared `conn` fixture from tests/conftest.py (the
rollback-on-teardown Postgres connection), overrides FastAPI's database
dependency so route handlers see the same in-flight transaction, seeds the
catalog + a tiny fixture using existing helpers, and asserts the HTTP
response shape.

No real data is committed; every test rolls back cleanly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn

from reimagina.data import aoi, feature as feat
from reimagina.indicators.catalog import seed_catalog
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
    """Enough connection to satisfy the route: it polls, so it ends read txns."""

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass


def _stub_connect(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _fake(dsn):
        yield _StubConn()

    monkeypatch.setattr("reimagina.db.connect", _fake)


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
    assert response.json() == {"run_id": "r-queued", "status": "queued"}


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
    assert response.json() == {"run_id": "r-done", "status": "succeeded"}
