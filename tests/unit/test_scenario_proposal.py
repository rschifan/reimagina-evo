"""`reimagina.scenarios.proposal`: the rules every scenario surface shares.

These used to live in `apps/cli/scenario.py`, which made them the command
line's rules by accident of who needed them first. The API router needs the
same ones and must not import from `apps/`, so the tests move with them —
and the one thing these tests check that the CLI's cannot is that the
messages are written for *no particular surface*.
"""

from __future__ import annotations

import math

import pytest

from reimagina.scenarios.authoring import InvalidGeometry
from reimagina.scenarios.ceilings import DENSITY, max_levels_for
from reimagina.scenarios.proposal import (
    CeilingPrecheckFailed,
    ProposalValueError,
    edits_for,
    polygon_wkt,
    precheck_ceilings,
    require_positive,
)

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}
OTHER_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[1.0, 0.0], [2.0, 0.0], [2.0, 1.0], [1.0, 1.0], [1.0, 0.0]]],
}


def _value(edits: list[dict], key: str) -> float:
    (edit,) = [e for e in edits if e["attribute_key"] == key]
    return edit["value_numeric"]


# ── the four edits ───────────────────────────────────────────────────────


def test_levels_writes_both_floors_and_num_levels():
    edits = edits_for(levels=6, unit_size=60, people_per_household=3.6)

    assert {e["attribute_key"] for e in edits} == {
        "floors",
        "num_levels",
        "unit_size",
        "people_per_household",
    }
    assert _value(edits, "floors") == _value(edits, "num_levels") == 6


def test_every_edit_key_is_authorable():
    """A key the authoring layer refuses is a four-way write that half-lands."""
    from reimagina.assets.registry import authorable_attributes

    keys = {e["attribute_key"] for e in edits_for(6, 60, 3.6)}
    assert keys == set(authorable_attributes())


# ── the two refusals, and whose words they are written in ────────────────


@pytest.mark.parametrize("value", [0.0, -3.0, float("nan"), float("inf")])
def test_a_degenerate_value_is_refused_and_names_its_field(value):
    with pytest.raises(ProposalValueError) as caught:
        require_positive(levels=value)

    assert caught.value.field == "levels"
    assert caught.value.value is value or math.isnan(value)


def test_the_precheck_carries_the_breach_and_the_allowance():
    """Structured, so a surface can render it rather than re-derive it."""
    unit_size = 60.0
    allowed = math.floor(max_levels_for(unit_size))

    precheck_ceilings(allowed, unit_size, 3.6)  # the boundary itself is legal

    with pytest.raises(CeilingPrecheckFailed) as caught:
        precheck_ceilings(allowed + 1, unit_size, 3.6)

    exc = caught.value
    assert [b.indicator_slug for b in exc.breaches] == [DENSITY]
    assert exc.allowed_levels == allowed
    assert (exc.levels, exc.unit_size) == (allowed + 1, unit_size)


@pytest.mark.parametrize(
    "raise_it",
    [
        lambda: require_positive(unit_size=-1.0),
        lambda: precheck_ceilings(10_000, 1.0, 3.6),
        lambda: polygon_wkt({"type": "Point", "coordinates": [0.0, 0.0]}),
    ],
    ids=["require_positive", "precheck_ceilings", "polygon_wkt"],
)
def test_the_domain_messages_name_fields_not_flags(raise_it):
    """⚠ The whole reason these functions moved out of `apps/cli`.

    A message carrying `--unit-size`, or telling the reader to run
    `scenario recompute`, is a message for someone at a terminal. Raised
    verbatim from an HTTP 400 it describes a command line the API client
    does not have and cannot run. The domain states the breach; each surface
    adds its own affordance — `apps/cli/scenario.py` re-raises with the flag
    names attached, which is what `test_scenario_cli.py` pins.
    """
    with pytest.raises((ValueError, InvalidGeometry)) as caught:
        raise_it()

    message = str(caught.value)
    assert "--" not in message
    assert "scenario recompute" not in message


def test_the_precheck_still_says_what_it_cannot_check():
    """Surface-neutral is not vaguer. A planner who passes must not read it
    as "this proposal is safe": the two area-scaled bounds are unexamined
    until materialisation, and the message has to say so in any dialect."""
    with pytest.raises(CeilingPrecheckFailed) as caught:
        precheck_ceilings(10_000, 1.0, 3.6)

    message = str(caught.value)
    assert "housing" in message and "population" in message
    assert "materialisation" in message


# ── the drawn area ───────────────────────────────────────────────────────


def test_polygon_wkt_accepts_a_bare_geometry():
    assert polygon_wkt(POLYGON) == "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"


def test_polygon_wkt_accepts_a_feature_and_a_feature_collection():
    """What a drawing tool exports, and what a request body carries."""
    feature = {"type": "Feature", "properties": {}, "geometry": POLYGON}

    assert polygon_wkt(feature).startswith("POLYGON ((0 0")
    assert polygon_wkt({"type": "FeatureCollection", "features": [feature]}).startswith(
        "POLYGON ((0 0"
    )


def test_polygon_wkt_unions_several_strokes_into_one_area():
    """A selection drawn in two strokes is one rule, not two."""
    wkt = polygon_wkt(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {}, "geometry": POLYGON},
                {"type": "Feature", "properties": {}, "geometry": OTHER_POLYGON},
            ],
        }
    )

    assert wkt.startswith("POLYGON ")  # not MULTIPOLYGON: they share an edge
    assert "2 1" in wkt and "2 0" in wkt


def test_polygon_wkt_refuses_input_with_no_polygonal_part():
    with pytest.raises(InvalidGeometry) as caught:
        polygon_wkt(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[0.0, 0.0], [1.0, 1.0]],
                        },
                    }
                ],
            }
        )

    assert "polygon" in str(caught.value).lower()
