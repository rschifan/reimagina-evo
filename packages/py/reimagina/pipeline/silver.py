"""Silver zone: clean, reproject, validate before loading into curated_geo."""

from __future__ import annotations
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.ops import transform as shapely_transform
from shapely.validation import make_valid

from reimagina.paths import data_dir

SILVER_DIR = data_dir() / "silver"


def _reproject_to_4326(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject to EPSG:4326.

    pyproj 3.7.x's vectorised transform path raises a NumPy ndim>0 scalar
    conversion DeprecationWarning when it receives a length-1 array, which
    geopandas hands it for single-geometry GeoDataFrames. Per-geometry
    shapely.ops.transform uses the scalar pyproj API and hits no such
    deprecation. We dispatch on len(gdf) == 1 so real-world loads stay on
    the fast vectorised path. See https://github.com/pyproj4/pyproj/issues
    for upstream progress; drop this branch once pyproj ships the fix.
    """
    if len(gdf) == 1:
        transformer = Transformer.from_crs(gdf.crs, "EPSG:4326", always_xy=True)
        reprojected = gdf.copy()
        reprojected["geometry"] = reprojected["geometry"].apply(
            lambda g: shapely_transform(transformer.transform, g)
            if g is not None
            else g
        )
        reprojected.set_crs("EPSG:4326", inplace=True, allow_override=True)
        return reprojected
    return gdf.to_crs(epsg=4326)


def clean_vector(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject to EPSG:4326 and repair invalid geometries.

    Rules applied (from DATA_LAYER.md Silver zone):
    - CRS must be set; raises ValueError otherwise.
    - Reproject to EPSG:4326 if not already there.
    - Repair invalid geometries with make_valid.
    - Drop null / empty geometries.
    """
    if gdf.crs is None:
        raise ValueError("GeoDataFrame has no CRS set — cannot clean")
    if gdf.crs.to_epsg() != 4326:
        gdf = _reproject_to_4326(gdf)

    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(
        lambda g: make_valid(g) if g is not None and not g.is_valid else g
    )
    # Filter nulls and empties via pandas operators instead of GeoSeries.notna,
    # which geopandas 0.14+ emits an informational UserWarning on whenever any
    # empty geometries are present in the series (even when the caller also
    # combines the mask with ~is_empty, as we do). pd.notna operates on the
    # underlying object array and bypasses the GeoSeries-specific warning
    # without changing semantics.
    geom = gdf["geometry"]
    keep_mask = pd.notna(geom) & ~geom.is_empty
    gdf = gdf[keep_mask].reset_index(drop=True)
    return gdf


def read_vector(path: str | Path) -> gpd.GeoDataFrame:
    """Read a vector file and return it cleaned.

    GeoParquet is preferred for large analytical subsets, while GeoJSON and
    other GDAL-supported formats remain supported for smaller/manual inputs.

    The data lake exposes its amenity layers with the ``.geoparquet``
    suffix; treat it as the same format as ``.parquet`` so callers don't
    have to fight the dispatch.
    """
    path = Path(path)
    if path.suffix.lower() in {".parquet", ".geoparquet"}:
        return clean_vector(gpd.read_parquet(path))
    return clean_vector(gpd.read_file(path))


def read_tabular(path: str | Path) -> pd.DataFrame:
    """Read a CSV tabular file."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported tabular format: {path.suffix!r}")


def save_silver_vector(gdf: gpd.GeoDataFrame, source_id: str, filename: str) -> Path:
    """Save a cleaned GeoDataFrame under ``data/silver/<source_id>/``.

    ``.parquet`` writes GeoParquet, which is the preferred Silver format for
    large vector subsets. Other extensions fall back to GDAL-backed writes.
    """
    dest = SILVER_DIR / source_id
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / filename
    if out.suffix.lower() == ".parquet":
        gdf.to_parquet(out)
    else:
        gdf.to_file(out, driver="GeoJSON")
    return out


def save_silver_tabular(df: pd.DataFrame, source_id: str, filename: str) -> Path:
    """Save a cleaned DataFrame to data/silver/<source_id>/<filename>.csv."""
    dest = SILVER_DIR / source_id
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / filename
    df.to_csv(out, index=False)
    return out
