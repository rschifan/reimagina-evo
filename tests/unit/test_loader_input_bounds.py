"""The supersede envelope comes from GeoParquet metadata when the file has it,
and from the geometries otherwise; both give the same 4326 bounds."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from reimagina.pipeline import loader


def _points(crs):
    return gpd.GeoDataFrame(
        {"a": [1, 2]}, geometry=[Point(-115.5, 32.6), Point(-115.4, 32.7)], crs=4326
    ).to_crs(crs)


def test_bounds_come_from_the_geoparquet_metadata(tmp_path, monkeypatch):
    path = tmp_path / "in.parquet"
    _points(4326).to_parquet(path)
    monkeypatch.setattr(
        loader.gpd, "read_parquet", lambda *_a, **_k: pytest.fail("read geometries")
    )

    assert loader._input_bounds_4326(path, None) == (-115.5, 32.6, -115.4, 32.7)


def test_bounds_fall_back_to_the_geometries_outside_lonlat(tmp_path):
    path = tmp_path / "in.parquet"
    _points(3857).to_parquet(path)

    assert loader._geoparquet_bounds_4326(path) is None
    assert loader._input_bounds_4326(path, None) == pytest.approx(
        (-115.5, 32.6, -115.4, 32.7)
    )


def test_a_plain_parquet_has_no_metadata_bounds(tmp_path):
    path = tmp_path / "in.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(path)

    assert loader._geoparquet_bounds_4326(path) is None
