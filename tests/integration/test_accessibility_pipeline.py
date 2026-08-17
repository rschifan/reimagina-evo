"""Accessibility end to end: both families, on a graph with known geometry.

The lattice has no diagonals, so a block and an amenity placed at opposite
corners of a square are 2 hops apart on the network but √2 apart in a straight
line. That gap is what separates a genuine routing result from a KNN wearing a
routing function's name — and it is asserted rather than eyeballed.

Amenities and blocks are pinned to lattice nodes so every expected distance is
a whole number of 95 m edges.
"""

from __future__ import annotations

from uuid import uuid4

from reimagina.data import feature as feat
from reimagina.indicators.accessibility import ACCESSIBILITY_COMPUTE
from reimagina.indicators.catalog import (
    ACCESSIBILITY_CUTOFF_M,
    seed_catalog,
)
from reimagina.pipeline.osm_network import load_graph
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    LATTICE_EDGE_M,
    insert_feature as _insert_feature,
    lattice_xy,
    osm_lattice,
    register_source as _register_source,
    start_run as _start_run,
)

_SIDE = 3
_EUCLID = "accessibility_block_school_euclid_m"
_NETWORK = "accessibility_block_school_network_m"


def _source(conn):
    source_id = _register_source(
        conn,
        name=f"acc_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    return source_id, _start_run(conn, source_id)


def _tiny_block_at(conn, col: int, row: int) -> str:
    """A block whose centroid sits essentially on the lattice node (col, row)."""
    lon, lat = lattice_xy(col, row)
    d = 0.00002  # ~2 m, so the centroid rounds to the node
    wkt = (
        f"POLYGON(({lon - d} {lat - d},{lon + d} {lat - d},"
        f"{lon + d} {lat + d},{lon - d} {lat + d},{lon - d} {lat - d}))"
    )
    source_id, ingest = _source(conn)
    return _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref=f"acc-blk-{uuid4().hex[:8]}",
        geometry_wkt=wkt,
        attributes={},
    )


def _amenity_at(conn, col: int, row: int, category: str) -> str:
    lon, lat = lattice_xy(col, row)
    source_id, ingest = _source(conn)
    return _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "global", "establishment"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref=f"acc-poi-{uuid4().hex[:8]}",
        geometry_wkt=f"POINT({lon} {lat})",
        attributes={"category": category},
    )


def _value(conn, feature_id: str, slug: str):
    row = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .get_current(feature_id=feature_id, indicator_slug=slug)
    )
    return None if row is None else row["value_numeric"]


def test_network_distance_exceeds_straight_line(conn):
    """The headline property, and the reason the network family exists.

    Block at (0,0), school at (1,1): two 95 m hops on the lattice, but only
    ~134 m as the crow flies. Anything reporting the straight line for the
    network indicator is not routing.
    """
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 1, 1, "school")

    ACCESSIBILITY_COMPUTE[_EUCLID](conn, feature_ids=[block], code_version="test")
    ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[block], code_version="test")

    euclid = _value(conn, block, _EUCLID)
    network = _value(conn, block, _NETWORK)

    assert network > euclid, "network distance must exceed the straight line"
    # Two lattice hops, within the tolerance of snapping a ~4 m block to a node.
    assert abs(network - 2 * LATTICE_EDGE_M) < 10
    # Straight line across one lattice square: 95 * sqrt(2).
    assert abs(euclid - LATTICE_EDGE_M * 2**0.5) < 10


def test_nearest_of_several_amenities_wins(conn):
    """Multi-source Dijkstra must report the closest, not the first or the last."""
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 2, 2, "school")  # 4 hops
    _amenity_at(conn, 1, 0, "school")  # 1 hop — the answer
    _amenity_at(conn, 0, 2, "school")  # 2 hops

    ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[block], code_version="test")
    assert abs(_value(conn, block, _NETWORK) - LATTICE_EDGE_M) < 10


def test_other_categories_are_ignored(conn):
    """A park two metres away must not answer the school question."""
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 0, 0, "park")  # on top of the block, wrong category
    _amenity_at(conn, 2, 0, "school")  # 2 hops away, right category

    ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[block], code_version="test")
    assert abs(_value(conn, block, _NETWORK) - 2 * LATTICE_EDGE_M) < 10


def test_no_amenity_of_that_category_reports_the_cutoff(conn):
    """Absence is reported as the cutoff, not as NULL and not as an error.

    array_agg over an empty set is NULL, and pgRouting raises on a NULL start
    array — so this exercises the short circuit, not just the arithmetic.
    """
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 1, 1, "park")  # no school anywhere

    for slug in (_EUCLID, _NETWORK):
        result = ACCESSIBILITY_COMPUTE[slug](
            conn, feature_ids=[block], code_version="test"
        )
        assert result.rows_written == 1
        assert _value(conn, block, slug) == ACCESSIBILITY_CUTOFF_M


def test_unreachable_block_reports_the_cutoff(conn):
    """Disconnected from the graph is the same answer as too far away."""
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    # Far outside the lattice: snaps to a corner node but is beyond the cutoff
    # from the amenity, so drivingDistance never reaches it.
    stranded = _tiny_block_at(conn, 40, 40)
    _amenity_at(conn, 0, 0, "school")

    ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[stranded], code_version="test")
    assert _value(conn, stranded, _NETWORK) == ACCESSIBILITY_CUTOFF_M


def test_offnetwork_block_pays_for_its_walk_to_the_street(conn):
    """The snap leg is part of the distance, not a free teleport.

    Regression guard. Without it a block anywhere off the graph snapped to the
    nearest node and reported that node's route, so a block kilometres from any
    road looked as accessible as one on the corner — plausible, confident, and
    wrong. Here the block sits ~1.5 lattice steps north of (0,0) with the school
    on that node, so the answer is essentially the walk to the network.
    """
    seed_catalog(conn)
    load_graph(conn, *osm_lattice(_SIDE))
    _amenity_at(conn, 0, 0, "school")

    lon, lat = lattice_xy(0, 0)
    d = 0.00002
    off_lat = lat - 1.5 * 0.0009  # ~143 m south of the lattice edge
    wkt = (
        f"POLYGON(({lon - d} {off_lat - d},{lon + d} {off_lat - d},"
        f"{lon + d} {off_lat + d},{lon - d} {off_lat + d},{lon - d} {off_lat - d}))"
    )
    source_id, ingest = _source(conn)
    block = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref=f"acc-off-{uuid4().hex[:8]}",
        geometry_wkt=wkt,
        attributes={},
    )

    ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[block], code_version="test")
    value = _value(conn, block, _NETWORK)

    # It snaps to node (0,0), where the school is — so the route is 0 and the
    # whole distance is the walk in. ~143 m, not 0.
    assert value > 100, f"snap distance was dropped: got {value}"
    assert abs(value - 1.5 * LATTICE_EDGE_M) < 25


def test_empty_road_network_raises_rather_than_reporting_cutoff(conn):
    """A database with no roads is a misconfiguration, not a finding.

    Reporting 1500 m everywhere would render as a plausible choropleth and hide
    the fact that nothing was ever loaded.
    """
    import pytest

    seed_catalog(conn)
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 1, 1, "school")
    # deliberately no load_graph

    with pytest.raises(RuntimeError, match="road_edge is empty"):
        ACCESSIBILITY_COMPUTE[_NETWORK](conn, feature_ids=[block], code_version="test")


def test_euclid_needs_no_road_network(conn):
    """The cheap family must not depend on the expensive family's input."""
    seed_catalog(conn)
    block = _tiny_block_at(conn, 0, 0)
    _amenity_at(conn, 1, 1, "school")

    result = ACCESSIBILITY_COMPUTE[_EUCLID](
        conn, feature_ids=[block], code_version="test"
    )
    assert result.rows_written == 1
    assert 0 < _value(conn, block, _EUCLID) < ACCESSIBILITY_CUTOFF_M
