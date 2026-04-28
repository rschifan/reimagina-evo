"""Unit tests for ``reimagina.pipeline.worldpop``.

DB-free. Mocks the WorldPop REST endpoint via ``monkeypatch`` on the
module-level ``_http_get`` helper so the real network never gets
touched, and uses a tiny synthetic in-memory GeoTIFF for the clipping
test. The integration test (which exercises the full ingest pipeline)
lives separately under ``tests/integration/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from reimagina.pipeline import worldpop


_FIXTURE_PAYLOAD = {
    "data": [
        {
            "id": "wpgp_MEX_2020",
            "title": "WorldPop · MEX · 2020",
            "popyear": "2020",
            "files": ["ftp://ftp.worldpop.org.uk/.../mex_ppp_2020_100m.tif"],
        },
        {
            "id": "wpgp_MEX_2010",
            "title": "WorldPop · MEX · 2010",
            "popyear": 2010,
            "files": ["ftp://ftp.worldpop.org.uk/.../mex_ppp_2010_100m.tif"],
        },
    ]
}


@pytest.fixture
def patched_http_get(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_http_get`` with a stub that returns the fixture
    payload regardless of URL. Tests can override per-call by passing
    a different stub via the returned setter."""
    holder: dict = {"payload": _FIXTURE_PAYLOAD, "calls": []}

    def stub(url: str) -> bytes:
        holder["calls"].append(url)
        return json.dumps(holder["payload"]).encode("utf-8")

    monkeypatch.setattr(worldpop, "_http_get", stub)
    return holder


# ─── Metadata discovery ───────────────────────────────────────────────


def test_discover_geotiff_url_returns_url_and_metadata(patched_http_get):
    meta = worldpop.discover_geotiff_url("MEX", 2020)
    assert meta.country == "MEX"
    assert meta.year == 2020
    assert meta.dataset == "wpgp"
    assert meta.url.endswith("mex_ppp_2020_100m.tif")
    assert meta.title == "WorldPop · MEX · 2020"
    # The catalogue endpoint receives the dataset slug + uppercased ISO3.
    assert any("/pop/wpgp?iso3=MEX" in u for u in patched_http_get["calls"])


def test_discover_geotiff_url_handles_string_popyear(patched_http_get):
    """WorldPop's catalogue mixes ``popyear`` types (some rows are int,
    others are string). The discoverer must coerce both."""
    meta = worldpop.discover_geotiff_url("MEX", 2010)
    assert meta.year == 2010
    assert meta.url.endswith("mex_ppp_2010_100m.tif")


def test_discover_geotiff_url_raises_when_year_missing(patched_http_get):
    with pytest.raises(worldpop.WorldPopFetchError, match="no row for year 1999"):
        worldpop.discover_geotiff_url("MEX", 1999)


def test_discover_geotiff_url_raises_when_files_empty(monkeypatch):
    """If the catalogue ever returns a row with an empty ``files`` array
    (mid-publication), bail with a clear message rather than producing
    an unusable Metadata."""
    payload = {
        "data": [{"popyear": 2020, "files": [], "id": "x", "title": "x"}],
    }
    monkeypatch.setattr(
        worldpop,
        "_http_get",
        lambda _url: json.dumps(payload).encode("utf-8"),
    )
    with pytest.raises(worldpop.WorldPopFetchError, match="no `files` entry"):
        worldpop.discover_geotiff_url("MEX", 2020)


# ─── Country raster fetch (caching) ───────────────────────────────────


def test_fetch_country_raster_uses_cache_on_second_call(
    monkeypatch,
    patched_http_get,
    tmp_path: Path,
):
    """A cached file with a matching ``(dataset, country, year)``
    filename must be reused — re-downloading a 900 MB country raster
    is the kind of slowdown the cache is for."""
    cache_dir = tmp_path / "wpcache"
    cache_dir.mkdir()
    cached_path = cache_dir / "wpgp_MEX_2020.tif"
    cached_path.write_bytes(b"FAKE-TIFF-PAYLOAD")

    download_calls: list[str] = []

    def fake_urlopen(*args, **kwargs):
        download_calls.append("urlopen")
        raise AssertionError("urlopen should not be called when cache is warm")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    out, meta = worldpop.fetch_country_raster(
        country="MEX",
        year=2020,
        cache_dir=cache_dir,
    )
    assert out == cached_path
    assert out.read_bytes() == b"FAKE-TIFF-PAYLOAD"
    assert meta.year == 2020
    assert download_calls == []


def test_fetch_country_raster_force_redownload(
    monkeypatch,
    patched_http_get,
    tmp_path: Path,
):
    """``force=True`` must re-fetch even when the cache is populated —
    operators use this to recover from a partial / corrupt download."""
    cache_dir = tmp_path / "wpcache"
    cache_dir.mkdir()
    cached_path = cache_dir / "wpgp_MEX_2020.tif"
    cached_path.write_bytes(b"OLD")

    class _FakeResponse:
        status = 200

        def __init__(self, data: bytes) -> None:
            self._data = data
            self._read = False

        def read(self, *args, **kwargs):
            if self._read:
                return b""
            self._read = True
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def fake_urlopen(_request, timeout=None):
        return _FakeResponse(b"NEW-PAYLOAD")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    out, _ = worldpop.fetch_country_raster(
        country="MEX",
        year=2020,
        cache_dir=cache_dir,
        force=True,
    )
    assert out.read_bytes() == b"NEW-PAYLOAD"


# ─── AOI clipping ─────────────────────────────────────────────────────


@pytest.fixture
def synthetic_raster(tmp_path: Path) -> Path:
    """4x4 single-band raster with predictable values so the clip test
    can assert that only the inner 2x2 cells survive a square polygon."""
    path = tmp_path / "input.tif"
    arr = np.array(
        [
            [1, 2, 3, 4],
            [5, 6, 7, 8],
            [9, 10, 11, 12],
            [13, 14, 15, 16],
        ],
        dtype=np.uint8,
    )
    transform = from_origin(west=0.0, north=4.0, xsize=1.0, ysize=1.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=arr.dtype,
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(arr, 1)
    return path


def test_clip_to_aoi_polygon_writes_smaller_raster(
    synthetic_raster: Path,
    tmp_path: Path,
):
    out = tmp_path / "clipped.tif"
    # Inner 2x2 square (cells with values 6, 7, 10, 11).
    wkt = "POLYGON ((1 1, 3 1, 3 3, 1 3, 1 1))"
    result = worldpop.clip_to_aoi_polygon(synthetic_raster, wkt, out)
    assert result == out
    with rasterio.open(out) as src:
        assert src.count == 1
        clipped = src.read(1)
    # Inner 2x2 of the original grid → 6, 7, 10, 11.
    assert sorted(clipped.flatten().tolist()) == [6, 7, 10, 11]


def test_clip_to_aoi_polygon_rejects_multi_band(tmp_path: Path):
    """The downstream ingest pipeline only handles single-band rasters,
    so we reject multi-band inputs here with a recognisable error
    rather than failing late inside ``ingest_raster_source``."""
    path = tmp_path / "multi.tif"
    arr = np.ones((2, 2), dtype=np.uint8)
    transform = from_origin(west=0.0, north=2.0, xsize=1.0, ysize=1.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=2,
        width=2,
        count=2,
        dtype=arr.dtype,
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(arr, 1)
        dst.write(arr, 2)

    with pytest.raises(worldpop.WorldPopFetchError, match="single-band"):
        worldpop.clip_to_aoi_polygon(
            path,
            "POLYGON ((0 0, 2 0, 2 2, 0 2, 0 0))",
            tmp_path / "out.tif",
        )


# ─── Orchestrator ─────────────────────────────────────────────────────


def test_fetch_without_aoi_copies_country_raster_through(
    monkeypatch,
    patched_http_get,
    tmp_path: Path,
):
    """Without ``aoi_geometry_wkt`` the orchestrator should land the
    country file at ``output_path`` byte-identical — no rasterio
    re-encode pass."""
    cache_dir = tmp_path / "wpcache"
    cache_dir.mkdir()
    cached = cache_dir / "wpgp_MEX_2020.tif"
    cached.write_bytes(b"PAYLOAD")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("should hit cache"),
    )

    out_path = tmp_path / "out.tif"
    summary = worldpop.fetch(
        country="MEX",
        year=2020,
        output_path=out_path,
        cache_dir=cache_dir,
    )
    assert out_path.read_bytes() == b"PAYLOAD"
    assert summary["clipped"] is False
    assert summary["cached"] is True
    assert summary["meta"]["year"] == 2020
