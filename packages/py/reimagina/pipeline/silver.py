"""Silver zone: clean, reproject, validate before loading into curated_geo."""
from __future__ import annotations
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.validation import make_valid

_REPO_ROOT = Path(__file__).resolve().parents[4]
SILVER_DIR = Path(os.environ.get("REIMAGINA_DATA_DIR", str(_REPO_ROOT / "data"))) / "silver"


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
        gdf = gdf.to_crs(epsg=4326)

    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(
        lambda g: make_valid(g) if g is not None and not g.is_valid else g
    )
    gdf = gdf[gdf["geometry"].notna() & ~gdf["geometry"].is_empty].reset_index(drop=True)
    return gdf


def read_vector(path: str | Path) -> gpd.GeoDataFrame:
    """Read a vector file and return it cleaned.

    GeoParquet is preferred for large analytical subsets, while GeoJSON and
    other GDAL-supported formats remain supported for smaller/manual inputs.
    """
    path = Path(path)
    if path.suffix.lower() == ".parquet":
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
