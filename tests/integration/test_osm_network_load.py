"""Loading an osmnx graph into the pgRouting tables, and routing over it.

No Overpass call: the frames here are hand-built in exactly the shape
``osmnx.graph_to_gdfs`` returns, so the database half is exercised without a
network round-trip or a flaky external dependency in CI. The fetch half is one
``osmnx`` call and is verified by hand against a real polygon.

The fixture is a 3x3 lattice of ~100 m blocks, which gives a graph where the
network distance between opposite corners is knowably longer than the
straight line — the property that separates a real routing result from a
straight-line one.
"""

from __future__ import annotations

import pytest

from reimagina.pipeline.osm_network import _osm_id, load_graph

from tests.integration._helpers import (
    LATTICE_EDGE_M,
    lattice_node_id as _node_id,
    osm_lattice,
)

_SIDE = 3


def _lattice():
    return osm_lattice(_SIDE)


def _route(conn, source_id: int, cutoff: float) -> dict[int, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node, agg_cost FROM pgr_drivingDistance(
              'SELECT id, source, target, cost, reverse_cost FROM curated_geo.road_edge',
              %s, %s, false)
            """,
            (source_id, cutoff),
        )
        return {int(node): float(cost) for node, cost in cur.fetchall()}


def test_load_writes_both_tables(conn):
    nodes, edges = _lattice()
    result = load_graph(conn, nodes, edges)

    assert result.nodes_written == _SIDE * _SIDE
    assert result.edges_written == 2 * _SIDE * (_SIDE - 1)
    assert result.total_length_m == pytest.approx(LATTICE_EDGE_M * result.edges_written)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM curated_geo.road_node")
        assert cur.fetchone()[0] == _SIDE * _SIDE
        cur.execute("SELECT count(*) FROM curated_geo.road_edge")
        assert cur.fetchone()[0] == 2 * _SIDE * (_SIDE - 1)


def test_load_is_idempotent(conn):
    """Re-loading replaces rather than accumulating.

    TRUNCATE ... CASCADE is what makes this true; without it a second ingest
    would double every edge and halve nothing, quietly inflating the graph.
    """
    nodes, edges = _lattice()
    load_graph(conn, nodes, edges)
    second = load_graph(conn, nodes, edges)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM curated_geo.road_edge")
        assert cur.fetchone()[0] == second.edges_written


def test_routing_respects_the_cutoff(conn):
    nodes, edges = _lattice()
    load_graph(conn, nodes, edges)

    corner = _node_id(0, 0)
    near = _route(conn, corner, 100.0)
    far = _route(conn, corner, 1000.0)

    # At 100 m only the origin and its two immediate neighbours are in range.
    assert set(near) == {corner, _node_id(1, 0), _node_id(0, 1)}
    assert len(far) == _SIDE * _SIDE  # the whole lattice
    assert max(far.values()) == pytest.approx(4 * LATTICE_EDGE_M)  # opposite corner


def test_network_distance_exceeds_straight_line(conn):
    """The property that proves this is routing, not a KNN in disguise.

    On a lattice the opposite corner is 4 hops (380 m) but only ~269 m away in
    a straight line, because the graph has no diagonal.
    """
    nodes, edges = _lattice()
    load_graph(conn, nodes, edges)

    opposite = _node_id(_SIDE - 1, _SIDE - 1)
    network_m = _route(conn, _node_id(0, 0), 1000.0)[opposite]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ST_Distance(a.geometry::geography, b.geometry::geography)
            FROM curated_geo.road_node a, curated_geo.road_node b
            WHERE a.id = %s AND b.id = %s
            """,
            (_node_id(0, 0), opposite),
        )
        straight_m = float(cur.fetchone()[0])

    assert network_m > straight_m
    assert network_m / straight_m == pytest.approx(1.41, abs=0.05)  # √2 detour


def test_dangling_edge_is_rejected_with_a_useful_message(conn):
    """A foreign-key violation mid-insert would blame the database, not the fetch."""
    nodes, edges = _lattice()
    edges.loc[0, "v"] = 999_999_999  # a node that was never fetched

    with pytest.raises(ValueError, match="absent from the node frame"):
        load_graph(conn, nodes, edges)


@pytest.mark.parametrize(
    "value,expected",
    [
        (12345, 12345),
        ([777, 888], 777),  # simplify=True merges ways into a list
        ((777, 888), 777),
        ([], None),
        (None, None),
        ("nonsense", None),
    ],
)
def test_osm_id_normalises_the_mixed_column(value, expected):
    assert _osm_id(value) == expected
