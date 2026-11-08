"""Seed a small synthetic territory so the platform can be run without the lake.

The ingestion pipelines read from a private Azure data lake. Anyone without
credentials to it — a reviewer, a new contributor, a CI job — can install the
platform, migrate it and start every process, and then has nothing to look at:
no features, so no indicators, no tiles and no scenario to evaluate.

This closes that gap without pretending to be real data. It generates a nested
territory on real Mexicali coordinates — AGEBs containing city blocks
containing buildings, plus an area of interest over the whole grid — and loads
it through the **same** writer the ingestion pipelines use —
``reimagina.data.feature.load_features`` — so what lands is validated by the
same triggers, carries the same provenance columns, and is indistinguishable in
shape from an ingested territory. Nothing here bypasses the loader.

What it is not: a substitute for the real dataset. The geometry is a grid, not
a city. Use it to exercise and demonstrate the platform, never to draw a
conclusion about Mexicali.

The nesting is the point: a single flat layer cannot exercise the parts of the
platform that matter most. Blocks carry ``parent_id`` to their AGEB, so
indicator rollup has a hierarchy to roll up *to*; the AOI gives every scoped
read surface — tiles, indicator overlays, AOI summaries — something to scope
to.

Usage::

    python scripts/synthetic_territory.py                 # 4x4 blocks, 2x2 AGEBs
    python scripts/synthetic_territory.py --blocks 6      # 6x6 blocks, 3x3 AGEBs
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from uuid import uuid4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _entry in (ROOT, os.path.join(ROOT, "packages", "py")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Central Mexicali. Blocks are laid out from here in a regular grid.
ORIGIN_LON = -115.4683
ORIGIN_LAT = 32.6245
BLOCK_M = 110.0  # side of one block, metres
STREET_M = 20.0  # gap between blocks, metres
# Street intersections get ids in a range no OSM node occupies, so a
# synthetic graph can never be mistaken for an ingested one.
NODE_ID_BASE = 9_000_000_000
# The layers that land in curated_geo.feature. The network tables and the
# AOI membership rows are counted separately because they are not features.
FEATURE_LAYERS = ("ageb", "block", "building", "establishment")


def _deg(metres: float, *, lat: float, east_west: bool) -> float:
    """Convert metres to degrees at this latitude. Good enough for a grid."""
    import math

    if east_west:
        return metres / (111_320.0 * math.cos(math.radians(lat)))
    return metres / 110_540.0


def build(blocks_per_side: int, *, ageb_side: int = 2):
    """Return (agebs, blocks, buildings) GeoDataFrames in EPSG:4326.

    ``ageb_side`` blocks on a side make up one AGEB. A partial AGEB at the
    edge of the grid is kept rather than dropped: real AGEBs are not square
    either, and a rollup that only ever sees full parents would not prove that
    the weighting handles uneven children.
    """
    import geopandas as gpd
    from shapely.geometry import Polygon

    pitch = BLOCK_M + STREET_M
    block_rows, building_rows = [], []
    # AGEB ref -> list of block corner tuples, so its extent is the union of
    # the blocks it actually got rather than an assumed square.
    ageb_bounds: dict[str, list[float]] = {}

    for row in range(blocks_per_side):
        for col in range(blocks_per_side):
            west = ORIGIN_LON + _deg(col * pitch, lat=ORIGIN_LAT, east_west=True)
            south = ORIGIN_LAT + _deg(row * pitch, lat=ORIGIN_LAT, east_west=False)
            east = west + _deg(BLOCK_M, lat=ORIGIN_LAT, east_west=True)
            north = south + _deg(BLOCK_M, lat=ORIGIN_LAT, east_west=False)
            ref = f"SYN{row:02d}{col:02d}"
            ageb_ref = f"SYNAGEB{row // ageb_side}{col // ageb_side}"
            box = ageb_bounds.setdefault(ageb_ref, [west, south, east, north])
            box[0], box[1] = min(box[0], west), min(box[1], south)
            box[2], box[3] = max(box[2], east), max(box[3], north)
            block_rows.append(
                {
                    "ref": ref,
                    "parent_ref": ageb_ref,
                    "name": f"Synthetic block {row}-{col}",
                    # Varied so indicators differ block to block rather than
                    # returning one value everywhere, which would prove nothing.
                    "floors": 2 + ((row + col) % 5),
                    "num_levels": 2 + ((row + col) % 5),
                    "unit_size": 65.0 + 5.0 * ((row * col) % 4),
                    "people_per_household": 3.2,
                    "geometry": Polygon(
                        [(west, south), (east, south), (east, north), (west, north)]
                    ),
                }
            )

            # Two to four buildings per block, inset from its edges.
            count = 2 + ((row * 3 + col) % 3)
            for b in range(count):
                span = BLOCK_M / (count + 1)
                bw = west + _deg(10 + b * span, lat=ORIGIN_LAT, east_west=True)
                bs = south + _deg(15, lat=ORIGIN_LAT, east_west=False)
                be = bw + _deg(span * 0.6, lat=ORIGIN_LAT, east_west=True)
                bn = bs + _deg(BLOCK_M * 0.5, lat=ORIGIN_LAT, east_west=False)
                building_rows.append(
                    {
                        "ref": f"{ref}B{b}",
                        "name": None,
                        "num_floors": 1 + (b % 4),
                        "geometry": Polygon([(bw, bs), (be, bs), (be, bn), (bw, bn)]),
                    }
                )

    # The AGEB polygon is the envelope of its blocks, grown by half a street so
    # the boundary falls in the roadway instead of along a block edge — on a
    # shared edge, "inside" is a floating-point coin toss.
    pad_x = _deg(STREET_M / 2, lat=ORIGIN_LAT, east_west=True)
    pad_y = _deg(STREET_M / 2, lat=ORIGIN_LAT, east_west=False)
    ageb_rows = [
        {
            "ref": ref,
            "name": f"Synthetic AGEB {ref[-2:]}",
            "geometry": Polygon(
                [
                    (w - pad_x, s_ - pad_y),
                    (e + pad_x, s_ - pad_y),
                    (e + pad_x, n + pad_y),
                    (w - pad_x, n + pad_y),
                ]
            ),
        }
        for ref, (w, s_, e, n) in sorted(ageb_bounds.items())
    ]

    return (
        gpd.GeoDataFrame(ageb_rows, geometry="geometry", crs="EPSG:4326"),
        gpd.GeoDataFrame(block_rows, geometry="geometry", crs="EPSG:4326"),
        gpd.GeoDataFrame(building_rows, geometry="geometry", crs="EPSG:4326"),
    )


def build_network(blocks_per_side: int):
    """Return (nodes, edges) GeoDataFrames in ``osmnx.graph_to_gdfs`` shape.

    The streets are the gaps the grid already leaves between blocks, drawn
    down their centre lines: one node per intersection, one edge per segment
    between adjacent intersections. ``load_graph`` wants a graph that is
    *already noded* — ids on the nodes, ``u``/``v``/``length`` on the edges —
    and that is exactly what a generated grid can give it, so nothing here
    needs ``pgr_createTopology``.

    Node ids are deliberately in the 9_000_000_000 range: the column holds
    OSM's own ids, and a synthetic id that could collide with a real one would
    be indistinguishable from it after the fact.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    pitch = BLOCK_M + STREET_M
    # One more line of streets than blocks, on each side, so the grid is
    # enclosed: blocks_per_side blocks have blocks_per_side + 1 streets around
    # them. The street centre line sits half a street before the first block.
    lines = blocks_per_side + 1
    half = STREET_M / 2

    def at(i: int, j: int) -> tuple[float, float]:
        """Lon/lat of the intersection on street column i, street row j."""
        return (
            ORIGIN_LON + _deg(i * pitch - half, lat=ORIGIN_LAT, east_west=True),
            ORIGIN_LAT + _deg(j * pitch - half, lat=ORIGIN_LAT, east_west=False),
        )

    def nid(i: int, j: int) -> int:
        return NODE_ID_BASE + j * 1000 + i

    node_rows = []
    for j in range(lines):
        for i in range(lines):
            lon, lat = at(i, j)
            node_rows.append({"osmid": nid(i, j), "x": lon, "y": lat})
    nodes = gpd.GeoDataFrame(
        node_rows,
        geometry=[Point(r["x"], r["y"]) for r in node_rows],
        crs="EPSG:4326",
    ).set_index("osmid")

    # Length in metres from the geography type rather than from BLOCK_M: the
    # two should agree, and taking the measured one means a mistake in the
    # degree conversion shows up as a wrong distance instead of being cancelled
    # out by the same mistake on both sides.
    edge_rows = []
    for j in range(lines):
        for i in range(lines):
            for di, dj in ((1, 0), (0, 1)):
                i2, j2 = i + di, j + dj
                if i2 >= lines or j2 >= lines:
                    continue
                a, b = at(i, j), at(i2, j2)
                edge_rows.append(
                    {
                        "u": nid(i, j),
                        "v": nid(i2, j2),
                        "osmid": None,
                        "geometry": LineString([a, b]),
                    }
                )
    edges = gpd.GeoDataFrame(edge_rows, geometry="geometry", crs="EPSG:4326")
    edges["length"] = edges.to_crs(32611).length  # UTM 11N — Mexicali's zone

    return nodes, edges


def build_amenities(blocks_per_side: int):
    """Return an establishment GeoDataFrame: one school, health post and park.

    Placed on three different intersections, deliberately not symmetrically,
    so network distance varies across the grid instead of coming out the same
    everywhere — a uniform answer cannot tell a working Dijkstra from a broken
    one.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    pitch = BLOCK_M + STREET_M
    half = STREET_M / 2
    last = blocks_per_side

    def at(i: int, j: int):
        return Point(
            ORIGIN_LON + _deg(i * pitch - half, lat=ORIGIN_LAT, east_west=True),
            ORIGIN_LAT + _deg(j * pitch - half, lat=ORIGIN_LAT, east_west=False),
        )

    # Interior intersections, not the outer ring: an amenity on the very edge
    # of the territory sits on the AOI boundary, where "inside" is decided by
    # floating point. Spread apart so network distance varies across the grid —
    # a uniform answer cannot tell a working Dijkstra from a broken one.
    inner = max(1, last - 1)
    placed = [
        ("school", "Synthetic school", 1, 1),
        ("health", "Synthetic health post", inner, max(1, last // 2)),
        ("park", "Synthetic park", max(1, last // 2), inner),
    ]
    return gpd.GeoDataFrame(
        [
            {
                "ref": f"SYNPOI-{category}",
                "name": name,
                "category": category,
                "geometry": at(i, j),
            }
            for category, name, i, j in placed
        ],
        geometry="geometry",
        crs="EPSG:4326",
    )


def write_raster(path: str, blocks_per_side: int, *, pixel_m: float = 5.0) -> dict:
    """Write a single-band population-count GeoTIFF over the grid.

    One more thing the lake holds and a reviewer does not: a raster. Without
    one, the raster half of the platform — COG staging, zonal statistics,
    aggregation recipes, the raster tile endpoint — cannot be exercised at all.

    The surface is a smooth bump centred on the grid, so zonal statistics come
    out different per block. **Population per pixel**, i.e. an *extensive*
    quantity, because that is the harder case: an extensive raster has to be
    summed and rescaled by pixel area, and a mistake there is invisible on an
    intensive one where the mean happens to be scale-free. Ingest it with
    ``--aggregation-kind extensive``.

    Returns the written file's shape and value range.
    """
    import math

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    pitch = BLOCK_M + STREET_M
    span_m = blocks_per_side * pitch
    west = ORIGIN_LON - _deg(STREET_M, lat=ORIGIN_LAT, east_west=True)
    south = ORIGIN_LAT - _deg(STREET_M, lat=ORIGIN_LAT, east_west=False)
    east = west + _deg(span_m + STREET_M, lat=ORIGIN_LAT, east_west=True)
    north = south + _deg(span_m + STREET_M, lat=ORIGIN_LAT, east_west=False)

    cols = max(16, int(span_m / pixel_m))
    rows = cols
    px = (east - west) / cols
    py = (north - south) / rows

    # A gaussian centred on the grid, sigma a third of its half-width, scaled
    # so a pixel at the peak holds ~12 people — a plausible figure for a 5 m
    # pixel of dense housing, which keeps the zonal sums readable.
    yy, xx = np.mgrid[0:rows, 0:cols]
    cx = cy = (cols - 1) / 2.0
    sigma = cols / 6.0
    band = 12.0 * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2)))
    band = band.astype("float32")

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(west, north, px, py),
        nodata=float("nan"),
    ) as dst:
        dst.write(band, 1)
        dst.set_band_description(1, "synthetic population per pixel")

    return {
        "path": path,
        "pixels": f"{cols}x{rows}",
        "pixel_m": round(pixel_m, 2),
        "value_min": float(band.min()),
        "value_max": float(band.max()),
        "total_population": float(band.sum()),
        "bbox": [west, south, east, north],
        "sigma_px": round(sigma, 1),
        "diagonal_m": round(math.hypot(span_m, span_m), 1),
    }


def seed(
    dsn: str, blocks_per_side: int, *, ageb_side: int = 2
) -> tuple[dict[str, int], str]:
    """Load the territory. Returns (count per layer, the new AOI's uuid)."""
    from reimagina.data import catalog as cat
    from reimagina.data.aoi import compute_aoi_features, create_aoi
    from reimagina.data.feature import load_features
    from reimagina.pipeline.osm_network import load_graph
    from reimagina.storage import backend_for_connection, connect

    agebs, blocks, buildings = build(blocks_per_side, ageb_side=ageb_side)
    amenities = build_amenities(blocks_per_side)
    nodes, edges = build_network(blocks_per_side)
    version = "synthetic-v1"
    wanted = {
        ("MEX", "ageb"),
        ("MEX", "block"),
        ("global", "building"),
        ("global", "establishment"),
    }

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select id, type_slug from curated_geo.feature_type_catalog "
            "where (context, type_slug) in %s",
            (tuple(wanted),),
        )
        type_ids = {slug: tid for tid, slug in cur.fetchall()}
        if len(type_ids) != len(wanted):
            raise SystemExit(
                "feature types missing — run `python -m apps.cli.app feat seed-types`"
            )

        source_id = cat.register_source(
            conn,
            name="Synthetic Mexicali territory",
            provider="reimagina-synthetic",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="generated://synthetic-mexicali",
            version=version,
            notes="Generated by scripts/synthetic_territory.py. Not real data.",
        )
        conn.commit()

        counts: dict[str, int] = {}
        # AGEBs first: blocks reference them, and load_features wants the
        # parent's uuid, not its source ref.
        layers: tuple[tuple[Any, str, list[str], str | None], ...] = (
            (agebs, "ageb", [], None),
            (
                blocks,
                "block",
                ["floors", "num_levels", "unit_size", "people_per_household"],
                "parent_id",
            ),
            (buildings, "building", ["num_floors"], None),
            # The POI layer the accessibility family reads. Its category lives
            # in attributes, which is where accessibility.py looks for it.
            (amenities, "establishment", ["category"], None),
        )
        for gdf, slug, attrs, parent_col in layers:
            if parent_col == "parent_id":
                cur.execute(
                    "select source_record_ref, id from curated_geo.feature "
                    "where source_id = %s and type_slug = 'ageb'",
                    (source_id,),
                )
                parents = dict(cur.fetchall())
                gdf = gdf.assign(parent_id=gdf["parent_ref"].map(parents))
                missing = int(gdf["parent_id"].isna().sum())
                if missing:
                    raise SystemExit(f"{missing} blocks have no AGEB parent")

            run_id = cat.start_ingestion_run(
                conn,
                source_id=source_id,
                orchestrator="synthetic",
                orchestrator_run_id=str(uuid4()),
                transformation_step=f"load_{slug}",
                code_version="synthetic",
            )
            conn.commit()
            rows = load_features(
                conn,
                gdf,
                source_id=source_id,
                ingestion_run_id=run_id,
                type_id=type_ids[slug],
                dataset_version=version,
                source_record_ref_col="ref",
                name_col="name" if "name" in gdf.columns else None,
                attributes_cols=attrs or None,
                parent_id_col=parent_col,
            )
            cat.complete_ingestion_run(
                conn,
                run_id,
                status="succeeded",
                rows_written=rows,
                dataset_version=version,
            )
            conn.commit()
            print(f"  {slug:9} {rows:>5} features")
            counts[slug] = rows

        # An AOI over the whole grid. Without one, every scoped read surface —
        # tiles with ?aoi_id, indicator overlays, AOI summaries — has nothing
        # to scope to and can only be exercised unscoped.
        project_id = (
            backend_for_connection(conn)
            .aoi_repo()
            .upsert_project_by_name(
                "Synthetic Mexicali",
                "Holds the synthetic territory. Not a real project.",
            )
        )
        conn.commit()
        # ST_Expand before ST_Envelope: an envelope drawn exactly on the
        # feature extent puts every edge feature on its own boundary, and
        # ST_Intersects there is a floating-point coin toss. ~50 m of margin
        # also matches how an AOI is actually drawn — around a territory,
        # not along its convex hull.
        cur.execute(
            "select ST_AsText(ST_Envelope(ST_Expand(ST_Extent(geometry)::geometry, 0.0005))) "
            "from curated_geo.feature where source_id = %s",
            (source_id,),
        )
        (envelope,) = cur.fetchone()
        aoi_id = create_aoi(
            conn,
            project_id=project_id,
            name="Synthetic Mexicali grid",
            geometry_wkt=envelope,
            description="Envelope of the generated territory.",
        )
        conn.commit()
        members = compute_aoi_features(conn, aoi_id)
        conn.commit()
        print(f"  aoi       {members:>5} member features  ({aoi_id})")
        counts["aoi_feature"] = members

        # The pedestrian graph, through the same loader the OSM ingest uses.
        # Without it the accessibility family refuses to compute — by design,
        # since an empty graph would otherwise report the cutoff everywhere.
        net = load_graph(conn, nodes, edges)
        conn.commit()
        print(
            f"  network   {net.nodes_written:>5} nodes, {net.edges_written} edges, "
            f"{net.total_length_m / 1000:.2f} km"
        )
        counts["road_node"] = net.nodes_written
        counts["road_edge"] = net.edges_written
    return counts, aoi_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=4, help="Blocks per side.")
    parser.add_argument(
        "--ageb-side", type=int, default=2, help="Blocks per side of one AGEB."
    )
    parser.add_argument(
        "--dsn", default=os.environ.get("DATABASE_URL", ""), help="Target database."
    )
    parser.add_argument(
        "--raster-out",
        default=None,
        metavar="PATH",
        help=(
            "Also write a synthetic single-band population GeoTIFF here, for "
            "`raster ingest --raw-path <PATH> --aggregation-kind extensive`."
        ),
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        print("No DSN. Set DATABASE_URL or pass --dsn.", file=sys.stderr)
        return 2
    counts, aoi_id = seed(args.dsn, args.blocks, ageb_side=args.ageb_side)
    features = sum(counts[k] for k in FEATURE_LAYERS if k in counts)
    print(f"  seeded {features} features into {args.dsn.rsplit('@', 1)[-1]}")
    print(f"  AOI_ID={aoi_id}")
    if args.raster_out:
        info = write_raster(args.raster_out, args.blocks)
        print(
            f"  raster    {info['pixels']} px @ ~{info['pixel_m']} m, "
            f"{info['total_population']:.0f} people total -> {info['path']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
