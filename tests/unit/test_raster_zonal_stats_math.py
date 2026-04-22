"""Unit tests for the zonal-stats math + coverage-threshold behaviour.

DB-free: builds a tiny in-memory raster with rasterio and exercises the
pure-Python record-building path (:func:`_compute_zonal_stats_records`).
The DB-side retire + bulk_upsert path is covered by the integration
suite — this file is about making sure the rasterstats call shape, the
coverage gating, and the lineage payload are right.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely import wkb as shp_wkb
from shapely.geometry import Polygon

from reimagina.jobs.raster_zonal_stats import _compute_zonal_stats_records


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def utm_raster(tmp_path: Path) -> Path:
    """Write a 10×10 UTM 14N raster with 30 m pixels.

    The raster is filled with a known ramp (row*10 + col) so any stat
    we compute has a hand-checkable expected value. NoData is -999.
    """
    path = tmp_path / "utm_ramp.tif"
    data = np.arange(100, dtype="float32").reshape(10, 10)
    transform = from_origin(500_000.0, 2_151_000.0, 30.0, 30.0)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=10, width=10,
        count=1, dtype="float32",
        crs="EPSG:32614",
        transform=transform,
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


def _utm_square_wkb(min_x: float, min_y: float, size: float) -> bytes:
    """Build a square polygon in UTM, then reproject its vertices to
    EPSG:4326 and return the WKB — matches what ``iter_geometries_by_type``
    returns from the DB (geometry column is stored in 4326)."""
    from pyproj import Transformer
    trans = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)
    corners_utm = [
        (min_x, min_y),
        (min_x + size, min_y),
        (min_x + size, min_y + size),
        (min_x, min_y + size),
        (min_x, min_y),
    ]
    corners_4326 = [trans.transform(x, y) for x, y in corners_utm]
    return shp_wkb.dumps(Polygon(corners_4326))


def _mock_backend(feature_wkbs: list[tuple[str, bytes]]) -> Mock:
    backend = Mock()
    backend.feature_repo.return_value.iter_geometries_by_type.return_value = (
        feature_wkbs
    )
    return backend


# ─── Tests ────────────────────────────────────────────────────────────

def test_records_have_attribute_keys_namespaced_by_prefix_and_stat(utm_raster):
    # Two pixels wide, aligned with the raster grid, so coverage is 100%.
    wkb = _utm_square_wkb(min_x=500_030.0, min_y=2_150_940.0, size=60.0)
    backend = _mock_backend([("feat-1", wkb)])

    records, in_scope, skipped = _compute_zonal_stats_records(
        backend=backend,
        feature_type_id="type-block",
        aoi_id=None,
        raster_path=str(utm_raster),
        asset_id="asset-1",
        attribute_key_prefix="temp",
        statistics=("mean", "p05"),
        coverage_threshold=0.5,
    )

    assert in_scope == 1
    assert skipped == 0
    keys = {r["attribute_key"] for r in records}
    assert keys == {"temp_mean", "temp_p05"}
    # Every record carries the prefix-agnostic lineage metadata the
    # observation table uses for debugging + QA traceback.
    for r in records:
        assert r["value_type"] == "numeric"
        assert r["lineage_ref"]["kind"] == "raster_zonal_stats"
        assert r["lineage_ref"]["asset_id"] == "asset-1"
        assert r["lineage_ref"]["pixels_valid"] > 0
        assert 0.0 <= r["lineage_ref"]["coverage_ratio"] <= 1.0


def test_feature_with_coverage_below_threshold_is_skipped(utm_raster, tmp_path):
    """A raster whose valid pixels are below threshold drops the whole
    feature — partial results would poison the is_current snapshot."""
    # Make a raster where only a small corner is valid (set everything
    # else to nodata), then intersect a feature that covers the whole
    # raster — expected coverage ≈ 4 valid / 100 total.
    path = tmp_path / "sparse.tif"
    data = np.full((10, 10), -999.0, dtype="float32")
    data[0:2, 0:2] = 42.0  # 4 valid pixels out of 100
    transform = from_origin(500_000.0, 2_151_000.0, 30.0, 30.0)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=10, width=10,
        count=1, dtype="float32",
        crs="EPSG:32614",
        transform=transform,
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)

    wkb = _utm_square_wkb(min_x=500_000.0, min_y=2_150_700.0, size=300.0)
    backend = _mock_backend([("feat-1", wkb)])

    records, in_scope, skipped = _compute_zonal_stats_records(
        backend=backend,
        feature_type_id="type-block",
        aoi_id=None,
        raster_path=str(path),
        asset_id="asset-1",
        attribute_key_prefix="temp",
        statistics=("mean",),
        coverage_threshold=0.5,
    )

    assert in_scope == 1
    assert skipped == 1
    assert records == []


def test_stats_value_is_correct_for_known_ramp(utm_raster):
    # Square covering all 100 pixels of the ramp raster (0..99).
    wkb = _utm_square_wkb(min_x=500_000.0, min_y=2_150_700.0, size=300.0)
    backend = _mock_backend([("feat-1", wkb)])

    records, _, _ = _compute_zonal_stats_records(
        backend=backend,
        feature_type_id="type-block",
        aoi_id=None,
        raster_path=str(utm_raster),
        asset_id="asset-1",
        attribute_key_prefix="val",
        statistics=("mean",),
        coverage_threshold=0.5,
    )

    mean_record = next(r for r in records if r["attribute_key"] == "val_mean")
    # The ramp is 0..99; the arithmetic mean is 49.5. Allow a sliver of
    # slop because the feature polygon, after reprojection through
    # EPSG:4326, may exclude a handful of edge pixels.
    assert 47.0 <= mean_record["value"] <= 52.0


def test_lineage_ref_pixels_valid_plus_nodata_equals_total(utm_raster, tmp_path):
    # Half nodata: verifies the coverage accounting is pixel-accurate.
    path = tmp_path / "half_nodata.tif"
    data = np.full((10, 10), -999.0, dtype="float32")
    data[:, 0:5] = 7.0  # 50 valid, 50 nodata
    transform = from_origin(500_000.0, 2_151_000.0, 30.0, 30.0)
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=10, width=10,
        count=1, dtype="float32",
        crs="EPSG:32614",
        transform=transform,
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)

    wkb = _utm_square_wkb(min_x=500_000.0, min_y=2_150_700.0, size=300.0)
    backend = _mock_backend([("feat-1", wkb)])

    records, _, _ = _compute_zonal_stats_records(
        backend=backend,
        feature_type_id="type-block",
        aoi_id=None,
        raster_path=str(path),
        asset_id="asset-1",
        attribute_key_prefix="val",
        statistics=("mean",),
        coverage_threshold=0.3,  # 50% coverage — stays above the bar
    )

    record = next(iter(records))
    lineage = record["lineage_ref"]
    total = lineage["pixels_valid"] + lineage["pixels_nodata"]
    # The coverage ratio is the direct ratio; sanity-check it matches.
    assert lineage["coverage_ratio"] == pytest.approx(
        lineage["pixels_valid"] / total, rel=1e-6,
    )
    # Half-nodata raster: ratio sits around 0.5 (polygon trim may shift
    # it by a few pixels).
    assert 0.4 <= lineage["coverage_ratio"] <= 0.6
