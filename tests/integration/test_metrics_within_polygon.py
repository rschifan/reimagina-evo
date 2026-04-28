"""Contract tests for ``POST /metrics/within-polygon``.

Wires the FastAPI app to the per-test transaction (rollback-on-teardown
in conftest.py), seeds two AOIs each with one block in different
geographies (Mexico City + Mexicali), then exercises the endpoint:

  - polygon over CDMX block + aoi_id=CDMX → count 1 for ``block``
  - polygon over CDMX block + aoi_id=Mexicali → count 0
    (AOI scoping bounds the search)
  - polygon over empty geography → count 0
  - polygon with NaN coordinates → 422
  - non-Polygon GeoJSON type → 422
  - empty type_slugs → empty results, no DB hit beyond the request
    parsing layer

Mirrors the seeding pattern of ``two_aois_one_block_each`` in
``test_api_routes.py`` so future grain additions stay consistent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.deps import get_db_conn

from reimagina.data import aoi, feature as feat


@pytest.fixture
def client(conn):
    """FastAPI TestClient pinned to the test transaction.

    ``initialise_pool=False`` skips the real pool — every ``get_db_conn``
    call routes back to the rollback-on-teardown ``conn`` fixture so
    seed inserts are visible to the route handler and nothing leaks to
    the DB after the test.
    """
    app = create_app(initialise_pool=False)

    def _override_conn():
        yield conn

    app.dependency_overrides[get_db_conn] = _override_conn
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def two_aois_two_blocks(conn):
    """One CDMX block + one Mexicali block, each in its own AOI.

    The two blocks live ~2000 km apart so polygons targeting one cannot
    accidentally match the other. AOI rows are inserted via the data
    layer; ``aoi_feature`` is inserted directly to keep the test
    independent of ``compute_overlaps`` geometry maths.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        insert_project as _insert_project,
        register_source as _register_source,
        start_run as _start_run,
    )

    source_id = _register_source(
        conn,
        name="metrics_within_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    block_cdmx = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="metrics-cdmx-blk",
        geometry_wkt=(
            "POLYGON((-99.140 19.430,-99.137 19.430,"
            "-99.137 19.433,-99.140 19.433,-99.140 19.430))"
        ),
    )
    block_mex = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="metrics-mex-blk",
        geometry_wkt=(
            "POLYGON((-115.470 32.640,-115.467 32.640,"
            "-115.467 32.643,-115.470 32.643,-115.470 32.640))"
        ),
    )

    project_cdmx = _insert_project(conn, name="metrics-proj-cdmx")
    project_mex = _insert_project(conn, name="metrics-proj-mex")
    aoi_cdmx = aoi.create_aoi(
        conn,
        project_id=project_cdmx,
        name="Metrics AOI CDMX",
        geometry_wkt="POLYGON((-99.20 19.40,-99.10 19.40,-99.10 19.50,-99.20 19.50,-99.20 19.40))",
    )
    aoi_mex = aoi.create_aoi(
        conn,
        project_id=project_mex,
        name="Metrics AOI Mexicali",
        geometry_wkt="POLYGON((-115.50 32.60,-115.40 32.60,-115.40 32.70,-115.50 32.70,-115.50 32.60))",
    )
    with conn.cursor() as cur:
        for aoi_id, feat_id in ((aoi_cdmx, block_cdmx), (aoi_mex, block_mex)):
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
        "aoi_cdmx": aoi_cdmx,
        "block_cdmx": block_cdmx,
        "aoi_mex": aoi_mex,
        "block_mex": block_mex,
    }


# Polygon enclosing the CDMX block and a healthy margin around it.
_POLYGON_OVER_CDMX_BLOCK = {
    "type": "Polygon",
    "coordinates": [
        [
            [-99.142, 19.428],
            [-99.135, 19.428],
            [-99.135, 19.435],
            [-99.142, 19.435],
            [-99.142, 19.428],
        ]
    ],
}

# Polygon over empty Pacific Ocean — no features anywhere.
_POLYGON_OVER_OCEAN = {
    "type": "Polygon",
    "coordinates": [
        [
            [-150.0, 0.0],
            [-149.9, 0.0],
            [-149.9, 0.1],
            [-150.0, 0.1],
            [-150.0, 0.0],
        ]
    ],
}


def test_within_polygon_counts_features_inside_aoi(client, two_aois_two_blocks):
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": _POLYGON_OVER_CDMX_BLOCK,
            "type_slugs": ["block"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload == {"results": [{"type_slug": "block", "count": 1}]}


def test_within_polygon_respects_aoi_scope(client, two_aois_two_blocks):
    """Same polygon as above but pinned to the Mexicali AOI — must
    return 0 because the CDMX block is not in Mexicali's aoi_feature."""
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_mex"],
            "polygon": _POLYGON_OVER_CDMX_BLOCK,
            "type_slugs": ["block"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"results": [{"type_slug": "block", "count": 0}]}


def test_within_polygon_returns_zero_for_empty_region(client, two_aois_two_blocks):
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": _POLYGON_OVER_OCEAN,
            "type_slugs": ["block"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"results": [{"type_slug": "block", "count": 0}]}


def test_within_polygon_preserves_request_order_for_multi_slug(
    client, two_aois_two_blocks
):
    """When the caller asks for multiple slugs the response preserves
    the request ordering so the panel's per-row layout is stable. Slugs
    not in the seeded data resolve to 0."""
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": _POLYGON_OVER_CDMX_BLOCK,
            "type_slugs": ["building", "block", "ageb"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert [row["type_slug"] for row in payload["results"]] == [
        "building",
        "block",
        "ageb",
    ]
    by_slug = {row["type_slug"]: row["count"] for row in payload["results"]}
    assert by_slug == {"building": 0, "block": 1, "ageb": 0}


def test_within_polygon_rejects_non_polygon_geojson(client, two_aois_two_blocks):
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": {"type": "Point", "coordinates": [-99.137, 19.432]},
            "type_slugs": ["block"],
        },
    )
    assert response.status_code == 422


def test_within_polygon_returns_empty_results_for_empty_type_slugs(
    client, two_aois_two_blocks
):
    fixture = two_aois_two_blocks
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": _POLYGON_OVER_CDMX_BLOCK,
            "type_slugs": [],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}


def test_within_polygon_rejects_nan_coordinates(client, two_aois_two_blocks):
    """``ST_GeomFromGeoJSON`` rejects NaN coordinates with
    ``InvalidParameterValue``; the router translates that to 422.
    Locks in the contract so a future psycopg2 / PostGIS exception-
    class change is caught.

    Python's stdlib ``json.dumps`` refuses to serialize NaN — we send
    raw JSON text with a literal ``NaN`` token (which most JSON
    parsers, including FastAPI's, accept as a JS-compatible
    extension) so the request reaches the API at all.
    """
    fixture = two_aois_two_blocks
    raw_body = (
        '{'
        f'  "aoi_id": "{fixture["aoi_cdmx"]}",'
        '   "polygon": {'
        '     "type": "Polygon",'
        '     "coordinates": [[[NaN, 19.428], [-99.135, 19.428],'
        '                      [-99.135, 19.435], [-99.142, 19.435],'
        '                      [NaN, 19.428]]]'
        '   },'
        '   "type_slugs": ["block"]'
        '}'
    )
    response = client.post(
        "/metrics/within-polygon",
        content=raw_body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_within_polygon_includes_boundary_touching_centroid(
    client, two_aois_two_blocks
):
    """Boundary-touching centroids must count as inside.

    The seeded CDMX block is the polygon
    ``((-99.140 19.430), (-99.137 19.430), (-99.137 19.433), (-99.140 19.433))``
    — its ``ST_PointOnSurface`` lands at the centre
    ``(-99.1385, 19.4315)``. This selection polygon has a vertical
    edge passing exactly through that centre x-coordinate
    (-99.1385). With ``ST_Contains`` the centroid would be on the
    boundary and excluded; with ``ST_Covers`` it counts. Locks in the
    inclusive contract so a future revert to ST_Contains breaks the
    test.
    """
    fixture = two_aois_two_blocks
    boundary_polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [-99.1385, 19.428],
                [-99.135, 19.428],
                [-99.135, 19.435],
                [-99.1385, 19.435],
                [-99.1385, 19.428],
            ]
        ],
    }
    response = client.post(
        "/metrics/within-polygon",
        json={
            "aoi_id": fixture["aoi_cdmx"],
            "polygon": boundary_polygon,
            "type_slugs": ["block"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"results": [{"type_slug": "block", "count": 1}]}
