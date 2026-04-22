"""Unit tests for reimagina.pipeline.raster COG conversion + metadata.

DB-free: builds tiny in-memory / on-disk rasters with rasterio and
verifies the pipeline's silver output is a valid COG with the right
metadata. The actual DB-side registration is covered by the integration
suite.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from reimagina.pipeline.raster import (
    UnsupportedRasterError,
    _resolution_in_metres,
)


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def tiny_plain_geotiff(tmp_path: Path) -> Path:
    """Write a 3×3 non-COG single-band GeoTIFF in EPSG:4326 (angular CRS)."""
    path = tmp_path / "tiny.tif"
    data = np.arange(9, dtype="float32").reshape(3, 3)
    transform = from_origin(-99.15, 19.45, 0.01, 0.01)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=3, width=3,
        count=1, dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def tiny_utm_geotiff(tmp_path: Path) -> Path:
    """Write a 3×3 non-COG GeoTIFF in EPSG:32614 (UTM 14N, metric)."""
    path = tmp_path / "tiny_utm.tif"
    data = np.arange(9, dtype="float32").reshape(3, 3)
    # 30m pixel; north-up, axis-aligned, square.
    transform = from_origin(500_000, 2_151_000, 30.0, 30.0)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=3, width=3,
        count=1, dtype="float32",
        crs="EPSG:32614",
        transform=transform,
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def two_band_geotiff(tmp_path: Path) -> Path:
    """Write a 2-band GeoTIFF — out of v1 scope; should raise on ingest."""
    path = tmp_path / "two_band.tif"
    data = np.arange(9, dtype="float32").reshape(3, 3)
    # Non-identity transform — rasterio errors on the identity-ish
    # affine under -W error because GDAL may silently drop it.
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=3, width=3,
        count=2, dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-99.15, 19.45, 0.01, 0.01),
    ) as dst:
        dst.write(data, 1)
        dst.write(data, 2)
    return path


# ─── resolution_m derivation rule (pure helper) ───────────────────────


def test_resolution_m_metric_crs_square_pixels(tiny_utm_geotiff):
    with rasterio.open(tiny_utm_geotiff) as src:
        assert _resolution_in_metres(src) == pytest.approx(30.0)


def test_resolution_m_is_none_for_angular_crs(tiny_plain_geotiff):
    """EPSG:4326 is angular (degrees) — resolution_m must be NULL."""
    with rasterio.open(tiny_plain_geotiff) as src:
        assert _resolution_in_metres(src) is None


def test_resolution_m_is_none_for_non_square_pixels(tmp_path):
    path = tmp_path / "rect.tif"
    with rasterio.open(
        path, "w",
        driver="GTiff", height=3, width=3, count=1, dtype="float32",
        crs="EPSG:32614",
        transform=from_origin(500_000, 2_151_000, 30.0, 10.0),  # 30×10
    ) as dst:
        dst.write(np.zeros((3, 3), dtype="float32"), 1)
    with rasterio.open(path) as src:
        assert _resolution_in_metres(src) is None


def test_resolution_m_is_none_for_rotated_transform(tmp_path):
    path = tmp_path / "rot.tif"
    from rasterio.transform import Affine
    # 45° rotation — non-zero b / d.
    rot = Affine.rotation(45)
    with rasterio.open(
        path, "w",
        driver="GTiff", height=3, width=3, count=1, dtype="float32",
        crs="EPSG:32614", transform=rot,
    ) as dst:
        dst.write(np.zeros((3, 3), dtype="float32"), 1)
    with rasterio.open(path) as src:
        assert _resolution_in_metres(src) is None


# ─── UnsupportedRasterError on >1 band ────────────────────────────────


def test_ingest_rejects_multi_band_raster(two_band_geotiff):
    """v1 scope is single-band only — a 2-band input must raise
    UnsupportedRasterError BEFORE any DB or filesystem side effects.

    The band-count check happens inside the initial ``rasterio.open()``
    up-front validation, ahead of ``backend_for_connection``. Passing
    a sentinel non-DB object is safe here — if the check fired late,
    the Postgres psycopg2 type-check inside ``backend_for_connection``
    would raise ``TypeError`` instead of our ``UnsupportedRasterError``.
    """
    from reimagina.pipeline.raster import ingest_raster_source

    with pytest.raises(UnsupportedRasterError, match="single-band only"):
        ingest_raster_source(
            conn=object(),  # never touched — the band check runs first
            raw_path=two_band_geotiff,
            source_name="x", provider="y", version="1",
            layer_slug="test_multiband",
        )


def test_ingest_rejects_missing_file():
    """FileNotFoundError with a clear message — checked before any DB work."""
    from reimagina.pipeline.raster import ingest_raster_source

    with pytest.raises(FileNotFoundError, match="Raw raster not found"):
        ingest_raster_source(
            conn=object(),
            raw_path=Path("/nowhere/does-not-exist.tif"),
            source_name="x", provider="y", version="1",
            layer_slug="missing",
        )


def test_ingest_rejects_bad_aggregation_kind(tiny_plain_geotiff):
    """aggregation_kind is validated against the same {'intensive','extensive'}
    set as the DB CHECK, so bad values fail fast rather than landing a
    source + file + raster_layer row that the CHECK then rejects."""
    from reimagina.pipeline.raster import ingest_raster_source

    with pytest.raises(ValueError, match="aggregation_kind"):
        ingest_raster_source(
            conn=object(),
            raw_path=tiny_plain_geotiff,
            source_name="x", provider="y", version="1",
            layer_slug="bad_kind",
            aggregation_kind="categorical",
        )


# ─── Happy-path COG conversion (no DB) ────────────────────────────────


def test_cog_conversion_produces_valid_cog(tiny_plain_geotiff, tmp_path):
    """The bronze→silver step re-encodes a plain GeoTIFF as a COG.

    We exercise the conversion directly (same call the pipeline makes)
    to avoid pulling in a full DB-backed integration test for a purely
    format-level property.
    """
    from rasterio.shutil import copy as rio_copy
    dst = tmp_path / "tiny.cog.tif"
    rio_copy(str(tiny_plain_geotiff), str(dst), driver="COG")

    assert dst.exists() and dst.stat().st_size > 0
    with rasterio.open(dst) as ds:
        # The COG driver round-trips as GTiff on read but tags the
        # dataset as COG via IMAGE_STRUCTURE.
        assert ds.driver in ("GTiff", "COG")
        tags = ds.tags(ns="IMAGE_STRUCTURE")
        # COG-driver output sets LAYOUT=COG in IMAGE_STRUCTURE tags.
        assert tags.get("LAYOUT") == "COG"
        # Data round-trips.
        assert ds.count == 1
        assert ds.read(1).shape == (3, 3)
        assert ds.crs.to_string() == "EPSG:4326"
