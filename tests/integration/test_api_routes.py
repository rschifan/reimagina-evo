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
    from tests.integration.test_data_layer_contracts import (
        _insert_feature, _register_source, _start_run,
    )
    seed_catalog(conn)
    source_id = _register_source(
        conn, name="api_route_src",
        storage_type="vector", integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type, source_id=source_id, ingestion_run_id=run_id,
        dataset_version="v1", source_record_ref="api-blk-1",
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
    compactness = next(r for r in payload if r["slug"] == "urban_form_block_compactness")
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
    from tests.integration.test_data_layer_contracts import _insert_project
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
    from tests.integration.test_data_layer_contracts import (
        _insert_feature, _register_source, _start_run,
    )
    source_id = _register_source(
        conn, name="api_features_src",
        storage_type="vector", integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    for i in range(3):
        _insert_feature(
            conn,
            type_id=block_type, source_id=source_id, ingestion_run_id=run_id,
            dataset_version="v1", source_record_ref=f"pg-{i}",
            geometry_wkt=f"POLYGON(({i} {i}, {i + 1} {i}, {i + 1} {i + 1}, {i} {i + 1}, {i} {i}))",
        )

    response = client.get("/features?type_slug=block&limit=2")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["features"]) == 2
    # Every row back is a block.
    assert all(f["type_slug"] == "block" for f in payload["features"])


def test_stats_endpoint_returns_percentiles(client, seeded_block_with_compactness):
    response = client.get(
        f"/indicators/{seeded_block_with_compactness['slug']}/stats"
    )
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
    response = client.get(
        "/indicators/urban_form_block_building_density/stats"
    )
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
    assert feature["properties"]["feature_id"] == seeded_block_with_compactness["block_id"]
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
    response = client.get(
        "/indicators/urban_form_block_compactness/values?limit=-1"
    )
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


def test_csv_download_respects_limit_override(
    client, seeded_block_with_compactness
):
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
