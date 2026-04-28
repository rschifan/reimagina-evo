"""Unit tests for pipeline routing logic — no database required."""

from __future__ import annotations

import pytest
import geopandas as gpd
from shapely.geometry import Point, Polygon

from reimagina.pipeline.silver import clean_vector


def test_clean_vector_reprojects_to_4326():
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Point(500000, 4000000)],  # UTM Zone 14N coords
        crs="EPSG:32614",
    )
    result = clean_vector(gdf)
    assert result.crs.to_epsg() == 4326


def test_clean_vector_fixes_invalid_geometry():
    # Bowtie / self-intersecting polygon → invalid
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    assert not bowtie.is_valid
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:4326")
    result = clean_vector(gdf)
    assert result.iloc[0].geometry.is_valid


def test_clean_vector_drops_null_geometries():
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(0, 0), None],
        crs="EPSG:4326",
    )
    result = clean_vector(gdf)
    assert len(result) == 1
    assert result.iloc[0]["id"] == 1


def test_clean_vector_drops_empty_geometries():
    from shapely.geometry import Point as ShapelyPoint

    empty = ShapelyPoint()  # empty point
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(1, 1), empty],
        crs="EPSG:4326",
    )
    result = clean_vector(gdf)
    assert len(result) == 1


def test_clean_vector_raises_without_crs():
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)])
    gdf = gdf.set_crs(None, allow_override=True)
    with pytest.raises(ValueError, match="no CRS"):
        clean_vector(gdf)


def test_clean_vector_preserves_already_4326():
    polygon = Polygon(
        [(-99.14, 19.43), (-99.13, 19.43), (-99.13, 19.44), (-99.14, 19.44)]
    )
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[polygon], crs="EPSG:4326")
    result = clean_vector(gdf)
    assert result.crs.to_epsg() == 4326
    assert len(result) == 1
