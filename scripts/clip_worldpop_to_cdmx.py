"""Clip the Mexico-wide WorldPop 100m raster to the CDMX boundary.

Pulls the CDMX boundary as the unioned municipality geometry from the
running reimagina_app Postgres DB (reprojected to the raster's CRS),
then writes a masked GeoTIFF ready for `reimagina raster ingest`.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import pyproj
import rasterio
import shapely.geometry.base
from rasterio.mask import mask
from shapely import wkb
from shapely.ops import transform


DSN = os.environ.get("REIMAGINA_APP_DSN", "postgresql://localhost/reimagina_app")
SRC = Path("data/sources/worldpop/mex_ppp_2020_100m.tif")
OUT = Path("data/local/worldpop_cdmx_2020_100m.tif")


def cdmx_boundary_wgs84() -> "shapely.geometry.base.BaseGeometry":
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        # Read from serving_geo (the consumer contract) rather than the
        # canonical curated_geo.feature — feature_v applies current-row
        # semantics and keeps downstream tools from accidentally unioning
        # historical versions of the same municipality. The CVEGEO
        # prefix '09' isolates the 16 CDMX alcaldías from any other
        # Mexican state's municipalities that might land in the same
        # DB later — without it this script would silently return the
        # union of every municipality loaded.
        cur.execute("""
            SELECT ST_AsBinary(ST_Transform(ST_Union(geometry), 4326))
            FROM serving_geo.feature_v
            WHERE type_slug = 'municipality'
              AND source_record_ref LIKE '09%'
        """)
        (wkb_bytes,) = cur.fetchone()
    return wkb.loads(bytes(wkb_bytes))


def main() -> None:
    geom_wgs84 = cdmx_boundary_wgs84()

    with rasterio.open(SRC) as src:
        # WorldPop is published in EPSG:4326; verify and reproject the
        # boundary if the raster happens to be in a different CRS.
        raster_crs = src.crs
        if raster_crs.to_string() != "EPSG:4326":
            to_raster = pyproj.Transformer.from_crs(
                "EPSG:4326", raster_crs, always_xy=True
            ).transform
            geom = transform(to_raster, geom_wgs84)
        else:
            geom = geom_wgs84

        out_image, out_transform = mask(
            src,
            [geom.__geo_interface__],
            crop=True,
            filled=True,
        )
        band_description = (
            src.descriptions[0] if src.descriptions else None
        ) or "population_count"
        out_meta = src.meta.copy()
        out_meta.update(
            {
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform,
                "compress": "deflate",
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(OUT, "w", **out_meta) as dst:
        dst.write(out_image)
        dst.set_band_description(1, band_description)

    print(f"wrote {OUT}  ({out_image.shape[2]}x{out_image.shape[1]}, CRS={raster_crs})")


if __name__ == "__main__":
    main()
