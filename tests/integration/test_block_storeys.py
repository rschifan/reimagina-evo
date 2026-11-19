"""Storeys per block, from the Overture buildings that sit in it.

A block's baseline storey count is the median ``num_floors`` of the buildings
whose point-on-surface falls inside it, written as a ``floors`` observation
(planned floors). ``num_levels``, the dwelling formula's storeys, is left to the
fallback layer: there it is fitted so a block's dwellings equal the census, and
a real height there would build homes on blocks the census found empty. A
block with no tagged building gets nothing here and keeps its fallback, which
``effective_numeric`` resolves after the observation.
"""

from __future__ import annotations

from uuid import uuid4

from reimagina.data import feature as feat
from reimagina.pipeline import block_storeys
from reimagina.scenarios.overrides import effective_numeric

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)


def _square(x: float, y: float, side: float) -> str:
    return (
        f"POLYGON(({x} {y},{x + side} {y},{x + side} {y + side},"
        f"{x} {y + side},{x} {y}))"
    )


def _feature(conn, src, run, type_id, wkt, attributes=None) -> str:
    return _insert_feature(
        conn,
        type_id=type_id,
        source_id=src,
        ingestion_run_id=run,
        dataset_version="v1",
        source_record_ref=f"storeys-{uuid4().hex[:8]}",
        geometry_wkt=wkt,
        attributes=attributes,
    )


def test_a_block_takes_the_median_storeys_of_its_buildings(conn):
    src = _register_source(
        conn,
        name=f"storeys_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run = _start_run(conn, src)
    block_type = feat.get_type_id(conn, "MEX", "block")
    building_type = feat.get_type_id(conn, "global", "building")

    # The demo fallback says 5 on both blocks; only the tagged one moves.
    tagged = _feature(
        conn,
        src,
        run,
        block_type,
        _square(-115.47, 32.62, 0.001),
        {"num_levels": 5, "floors": 5},
    )
    untagged = _feature(
        conn,
        src,
        run,
        block_type,
        _square(-115.46, 32.62, 0.001),
        {"num_levels": 5, "floors": 5},
    )
    for x, floors in ((-115.4699, 1), (-115.4697, 2), (-115.4695, 4)):
        _feature(
            conn,
            src,
            run,
            building_type,
            _square(x, 32.6201, 0.0001),
            {"num_floors": floors},
        )
    # Inside but untagged, and tagged but outside: neither counts.
    _feature(conn, src, run, building_type, _square(-115.4693, 32.6201, 0.0001))
    _feature(
        conn,
        src,
        run,
        building_type,
        _square(-115.45, 32.6201, 0.0001),
        {"num_floors": 9},
    )
    # Inside the untagged block, but no num_floors.
    _feature(conn, src, run, building_type, _square(-115.4599, 32.6201, 0.0001))

    result = block_storeys.bootstrap(conn, feature_ids=[tagged, untagged])

    assert result["blocks_written"] == 1
    floors = effective_numeric(
        conn, type_id=block_type, attribute_key="floors", feature_ids=[tagged, untagged]
    )
    assert floors == {tagged: 2.0, untagged: 5.0}
    levels = effective_numeric(
        conn,
        type_id=block_type,
        attribute_key="num_levels",
        feature_ids=[tagged, untagged],
    )
    assert levels == {tagged: 5.0, untagged: 5.0}


def test_storeys_round_to_a_whole_storey_and_never_below_one(conn):
    src = _register_source(
        conn,
        name=f"storeys_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run = _start_run(conn, src)
    block_type = feat.get_type_id(conn, "MEX", "block")
    building_type = feat.get_type_id(conn, "global", "building")

    halves = _feature(conn, src, run, block_type, _square(-115.47, 32.63, 0.001))
    zero = _feature(conn, src, run, block_type, _square(-115.46, 32.63, 0.001))
    # Median of 2 and 3 is 2.5: a storey count is whole, and half rounds up.
    for x, floors in ((-115.4699, 2), (-115.4697, 3)):
        _feature(
            conn,
            src,
            run,
            building_type,
            _square(x, 32.6301, 0.0001),
            {"num_floors": floors},
        )
    _feature(
        conn,
        src,
        run,
        building_type,
        _square(-115.4599, 32.6301, 0.0001),
        {"num_floors": 0},
    )

    block_storeys.bootstrap(conn, feature_ids=[halves, zero])

    values = effective_numeric(
        conn, type_id=block_type, attribute_key="floors", feature_ids=[halves, zero]
    )
    assert values == {halves: 3.0, zero: 1.0}
