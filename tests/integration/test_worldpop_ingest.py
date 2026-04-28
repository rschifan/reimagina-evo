"""Integration tests for the WorldPop API → raster ingest handoff.

The unit suite (`tests/unit/test_worldpop_pipeline.py`) covers metadata
discovery + caching + clipping in isolation. This file exercises the
CLI-shaped `fetch_worldpop_raster()` glue against a real Postgres DB:

- AOI lookup: pulls the polygon WKT from ``curated_geo.area_of_interest``.
- Network: mocked to return a local synthetic raster as the WorldPop
  catalogue's data file (file:// URI), so the test stays offline.
- Ingest handoff: the clipped GeoTIFF is fed into
  ``ingest_raster_source`` exactly like the operator would in the
  runbook, verifying the contract the two pipelines share.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from apps.cli.raster import fetch_worldpop_raster
from reimagina.data import aoi as aoi_mod
from reimagina.pipeline import worldpop
from reimagina.pipeline.raster import ingest_raster_source
from reimagina.storage import backend_for_connection


# ─── Filesystem isolation (mirrors test_raster_ingest.isolated_data_dir) ──


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect bronze + silver writes to ``tmp_path`` so the test
    doesn't touch the repo's real data tree. Same shape as the fixture
    used by ``test_raster_ingest`` — kept local here to avoid a
    cross-test fixture dependency."""
    import reimagina.pipeline.bronze as bronze_mod
    import reimagina.pipeline.silver as silver_mod
    import reimagina.pipeline.raster as raster_mod

    bronze_dir = tmp_path / "bronze"
    silver_dir = tmp_path / "silver"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    silver_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bronze_mod, "BRONZE_DIR", bronze_dir)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setattr(raster_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setenv("REIMAGINA_DATA_DIR", str(tmp_path))
    yield tmp_path


# ─── Synthetic country raster ─────────────────────────────────────────


def _write_country_raster(path: Path) -> Path:
    """6x6 EPSG:4326 raster covering 0..6 lon, 0..6 lat. Values = row*6+col
    so the clip test can assert exact pixel coverage."""
    arr = np.arange(36, dtype="float32").reshape(6, 6)
    transform = from_origin(west=0.0, north=6.0, xsize=1.0, ysize=1.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=6,
        width=6,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(arr, 1)
    return path


# ─── DB fixtures ──────────────────────────────────────────────────────


def _ensure_test_project(conn) -> str:
    """Insert a fresh project row. ``app_core.project`` has no UNIQUE on
    ``name`` so we just use a uuid-suffixed name and skip ON CONFLICT."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_core.project (name, description) "
            "VALUES (%s, %s) RETURNING id::text",
            (f"test-worldpop-{uuid4().hex[:8]}", "worldpop ingest integration test"),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def aoi_row(conn) -> tuple[str, str]:
    """Insert an AOI whose polygon covers the inner 4x4 of the synthetic
    country raster (i.e. lon 1..5, lat 1..5). The clip should keep
    rows 1-4, cols 1-4 → 16 pixels.

    The fetch helper opens a separate DB connection (it has to — the
    CLI is not in a transaction context), so the AOI must be committed
    rather than rolled back at teardown. Cleanup at fixture exit
    removes the row so we don't leak state across the test session.
    """
    project_id = _ensure_test_project(conn)
    aoi_geometry_wkt = "POLYGON ((1 1, 5 1, 5 5, 1 5, 1 1))"
    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=project_id,
        name=f"worldpop-test-aoi-{uuid4().hex[:8]}",
        geometry_wkt=aoi_geometry_wkt,
        description="synthetic AOI for worldpop ingest integration test",
    )
    conn.commit()
    yield aoi_id, aoi_geometry_wkt
    with conn.cursor() as cur:
        cur.execute("DELETE FROM curated_geo.area_of_interest WHERE id = %s", (aoi_id,))
        cur.execute("DELETE FROM app_core.project WHERE id = %s", (project_id,))
    conn.commit()


# ─── Network mocking ──────────────────────────────────────────────────


@pytest.fixture
def mocked_worldpop_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Replace ``_http_get`` (catalogue) and ``urlopen`` (download) so
    the WorldPop fetch resolves to a local synthetic raster. Returns
    the path of the synthetic country raster the catalogue points at,
    so the caller can assert on its contents downstream."""
    country_raster = _write_country_raster(tmp_path / "country.tif")

    payload = {
        "data": [
            {
                "id": "test-row",
                "title": "synthetic test country raster",
                "popyear": 2020,
                "files": [country_raster.as_uri()],  # file:// URI handled by urllib
            }
        ]
    }
    monkeypatch.setattr(
        worldpop,
        "_http_get",
        lambda _url: json.dumps(payload).encode("utf-8"),
    )
    return country_raster


# ─── The actual handoff test ──────────────────────────────────────────


def test_fetch_worldpop_then_ingest_round_trips_through_curated_geo(
    conn,
    db_url,
    aoi_row,
    mocked_worldpop_api: Path,
    isolated_data_dir: Path,
    tmp_path: Path,
):
    dsn = db_url
    aoi_id, _aoi_wkt = aoi_row
    output_path = tmp_path / "wpgp_clipped.tif"
    cache_dir = tmp_path / "cache"

    # Step 1 — operator runs `raster fetch-worldpop --clip --aoi-id ...`.
    summary = fetch_worldpop_raster(
        country="MEX",
        year=2020,
        output_path=output_path,
        cache_dir=cache_dir,
        aoi_id=aoi_id,
        dsn=dsn,
    )
    assert summary["clipped"] is True
    assert Path(summary["output_path"]) == output_path
    assert output_path.exists()

    with rasterio.open(output_path) as src:
        assert src.count == 1
        clipped = src.read(1)
    # Original 6x6, AOI = lon 1..5 / lat 1..5 → inner 4x4 = 16 pixels.
    assert clipped.size == 16

    # Step 2 — operator runs `raster ingest --raw-path <output_path>`.
    # Identical contract to a hand-written GeoTIFF; this is the test
    # that proves the two pipelines are decoupled at the file boundary.
    result = ingest_raster_source(
        conn,
        raw_path=output_path,
        source_name=f"worldpop-test-{uuid4().hex[:6]}",
        provider="WorldPop",
        version="2020-test",
        layer_slug=f"worldpop_test_{uuid4().hex[:6]}",
        aggregation_kind="extensive",
        orchestrator_run_id=str(uuid4()),
        code_version="test",
    )
    conn.commit()

    # raster_layer row visible through the existing repo surface.
    backend = backend_for_connection(conn)
    layers = backend.raster_layer_repo().list_published_layers()
    matching = [row for row in layers if str(row["id"]) == result.layer_id]
    assert len(matching) == 1
    layer_row = matching[0]
    assert layer_row["aggregation_kind"] == "extensive"

    # Silver COG materialised on disk and rasterio can re-open it.
    silver_path = Path(result.silver_path)
    assert silver_path.exists()
    with rasterio.open(silver_path) as src:
        assert src.count == 1
        # COG re-encode preserves the clipped extent.
        assert src.shape == clipped.shape
