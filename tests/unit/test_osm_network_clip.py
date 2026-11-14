"""Clipping the OSM fetch boundary without changing the AOI.

An AOI is an administrative unit and some are mostly empty. Municipio de
Mexicali is 9,971 km² reaching the Sea of Cortez at San Felipe, nearly all of
it desert with no pedestrian network. Asking Overpass for a walking graph over
that polygon costs minutes and gigabytes for a ~99 % empty result — and a
truncated fetch is indistinguishable from a city with no streets.

`clip_polygon` narrows what is fetched. It must not be confused with narrowing
the AOI: membership, overlaps and every indicator still scope to the full
municipal boundary, which is why the clip lives in the fetch path and nowhere
near `create_aoi`.
"""

from __future__ import annotations

import pytest
from shapely.geometry import box

from reimagina.pipeline.osm_network import clip_polygon
from reimagina.pipeline.overture import MEXICALI_BBOX


#: Stands in for the real municipio: tall, mostly empty, reaching far south.
#: The real one is -115.882,31.363 -> -114.650,32.719.
MUNICIPIO = box(-115.882, 31.363, -114.650, 32.719)


def test_clipping_keeps_only_the_overlap():
    clipped = clip_polygon(MUNICIPIO, MEXICALI_BBOX)
    assert not clipped.is_empty
    xmin, ymin, xmax, ymax = clipped.bounds
    # The clip cannot reach outside either shape.
    assert xmin >= min(MEXICALI_BBOX[0], MUNICIPIO.bounds[0]) - 1e-9
    assert ymax <= max(MEXICALI_BBOX[3], MUNICIPIO.bounds[3]) + 1e-9
    assert clipped.within(MUNICIPIO.buffer(1e-9))


def test_clipping_is_what_makes_the_fetch_tractable():
    """The number that justifies the feature existing."""
    fraction = clip_polygon(MUNICIPIO, MEXICALI_BBOX).area / MUNICIPIO.area
    # ~0.5 x 0.32 degrees kept out of ~1.23 x 1.36. An unclipped fetch is an
    # order of magnitude more area, essentially all of it empty desert.
    assert fraction < 0.15, (
        f"the clip keeps {fraction:.1%} of the municipio; if this ever "
        f"approaches 1.0 the clip has stopped doing anything and the Overpass "
        f"request is municipio-sized again"
    )


def test_a_non_overlapping_bbox_raises_rather_than_fetching_nothing():
    """An empty intersection is a mistake, not a small graph.

    Loading an empty graph would truncate `road_node CASCADE` and leave the
    platform with no network at all — routable nothing, and `road_edge` empty
    is exactly what the accessibility guard reports as "run the loader". So the
    user would be told to do the thing they just did.
    """
    with pytest.raises(ValueError, match="does not intersect"):
        clip_polygon(MUNICIPIO, (-99.36, 19.05, -98.94, 19.59))  # CDMX


def test_the_error_names_the_two_things_worth_checking():
    with pytest.raises(ValueError) as exc:
        clip_polygon(MUNICIPIO, (-99.36, 19.05, -98.94, 19.59))
    message = str(exc.value)
    assert "xmin, ymin, xmax, ymax" in message, "ordering is the usual mistake"
    assert "city" in message


def test_a_bbox_containing_the_aoi_is_a_no_op_not_an_error():
    # Passing a generous box should be harmless; only a disjoint one is a bug.
    whole_world = (-180.0, -90.0, 180.0, 90.0)
    clipped = clip_polygon(MUNICIPIO, whole_world)
    assert clipped.area == pytest.approx(MUNICIPIO.area)
