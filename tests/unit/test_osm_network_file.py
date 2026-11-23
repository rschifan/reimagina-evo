"""A fetched OSM graph saved to files loads back unchanged, with no network.

The Mexicali source bundle ships the walking graph as two parquet files so an
install does not depend on Overpass. What ``load_graph`` reads from the files
must be what it would have read from the fetch: same node ids and coordinates,
same edge endpoints, lengths, geometries and OSM way ids.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from reimagina.pipeline.osm_network import read_graph, save_graph


def _graph():
    nodes = gpd.GeoDataFrame(
        {"x": [0.0, 1.0, 2.0], "y": [0.0, 1.0, 0.5]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 0.5)],
        index=pd.Index([10, 11, 12], name="osmid"),
        crs="EPSG:4326",
    )
    edges = gpd.GeoDataFrame(
        {
            "u": [10, 11],
            "v": [11, 12],
            "key": [0, 0],
            # simplify=True leaves a list where several ways were merged.
            "osmid": [[100, 101], 102],
            "length": [157.2, 120.5],
        },
        geometry=[LineString([(0, 0), (1, 1)]), LineString([(1, 1), (2, 0.5)])],
        crs="EPSG:4326",
    )
    return nodes, edges


def test_saved_graph_reads_back_what_load_graph_needs(tmp_path):
    nodes, edges = _graph()
    save_graph(nodes, edges, tmp_path, fetched="2026-11-10")

    got_nodes, got_edges, fetched = read_graph(tmp_path)

    assert fetched == "2026-11-10"
    assert list(got_nodes.index) == [10, 11, 12]
    assert list(got_nodes["x"]) == [0.0, 1.0, 2.0]
    assert list(got_nodes["y"]) == [0.0, 1.0, 0.5]
    rows = list(got_edges.itertuples(index=False))
    assert [(r.u, r.v, r.length, r.osmid) for r in rows] == [
        (10, 11, 157.2, 100),
        (11, 12, 120.5, 102),
    ]
    # Python ints: load_graph's _osm_id drops anything else as "no id".
    assert all(type(r.osmid) is int for r in rows)
    assert [r.geometry.wkb for r in rows] == list(edges.geometry.to_wkb())


def test_saving_is_byte_deterministic(tmp_path):
    nodes, edges = _graph()
    save_graph(nodes, edges, tmp_path / "a", fetched="2026-11-10")
    save_graph(nodes, edges, tmp_path / "b", fetched="2026-11-10")
    for name in ("nodes.parquet", "edges.parquet"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
