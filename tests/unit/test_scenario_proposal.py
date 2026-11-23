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
    RULE_INPUTS,
    CeilingPrecheckFailed,
    ProposalValueError,
    changed_value,
    check_inputs,
    rule_units,
    edits_for,
    polygon_wkt,
    precheck_ceilings,
    reachable_indicators,
    table_statistic,
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

    keys = {e["attribute_key"] for e in edits_for(**dict.fromkeys(RULE_INPUTS, 1.0))}
    assert keys == set(authorable_attributes())


# ── the two refusals, and whose words they are written in ────────────────


@pytest.mark.parametrize("value", [0.0, -3.0, float("nan"), float("inf")])
def test_a_degenerate_value_is_refused_and_names_its_field(value):
    with pytest.raises(ProposalValueError) as caught:
        check_inputs({"levels": value})

    assert caught.value.field == "levels"
    assert caught.value.value == value or math.isnan(value)


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
        lambda: check_inputs({"unit_size": -1.0}),
        lambda: precheck_ceilings(10_000, 1.0, 3.6),
        lambda: polygon_wkt({"type": "Point", "coordinates": [0.0, 0.0]}),
    ],
    ids=["check_inputs", "precheck_ceilings", "polygon_wkt"],
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


@pytest.mark.parametrize(
    ("inputs", "field"),
    [
        ({"unit_size": 0.5}, "unit_size"),
        ({"unit_size": 15}, "unit_size"),
        ({"people_per_household": 1000}, "people_per_household"),
        ({"storeys": 3}, "storeys"),
        ({"levels": True}, "levels"),
        ({"levels": "3"}, "levels"),
    ],
    ids=["tiny-unit", "unit-at-bound", "huge-household", "unknown", "bool", "text"],
)
def test_implausible_or_unknown_inputs_are_refused_by_name(inputs, field):
    """0.5 m² per dwelling or 1,000 people per household used to pass, and
    failed later as a per-block breach naming a block id, not a field."""
    with pytest.raises(ProposalValueError) as caught:
        check_inputs(inputs)
    assert caught.value.field == field


def test_check_inputs_keeps_only_the_numbers_set_in_registry_order():
    assert check_inputs({"lot_coverage": 0.5, "levels": 3, "unit_size": None}) == {
        "levels": 3.0,
        "lot_coverage": 0.5,
    }


# ── the drawn area ───────────────────────────────────────────────────────


def test_polygon_wkt_drops_a_third_coordinate():
    """QGIS and GPS exports carry Z; the column is 2D and the insert was a 500."""
    z = {
        "type": "Polygon",
        "coordinates": [[[0, 0, 5], [1, 0, 5], [1, 1, 5], [0, 1, 5], [0, 0, 5]]],
    }
    assert polygon_wkt(z) == "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"


@pytest.mark.parametrize(
    "ring",
    [
        # Latitude and longitude swapped for Mexicali: latitude -115 is no
        # latitude at all.
        [[32.6, -115.5], [32.7, -115.5], [32.7, -115.4], [32.6, -115.5]],
        [[-500, 95], [-499, 95], [-499, 96], [-500, 95]],
        # Projected metres (UTM), stored as SRID 4326, would select nothing.
        [[500000, 2100000], [500100, 2100000], [500100, 2100100], [500000, 2100000]],
        [[0, 0], [0, float("nan")], [1, 1], [0, 0]],
    ],
    ids=["swapped", "out-of-range", "projected", "nan"],
)
def test_polygon_wkt_refuses_what_is_not_lon_lat_degrees(ring):
    with pytest.raises(InvalidGeometry, match="longitude/latitude"):
        polygon_wkt({"type": "Polygon", "coordinates": [ring]})


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


@pytest.mark.parametrize(
    ("malformed", "original"),
    [
        ({"type": "Polygon"}, KeyError),
        ({"type": "Nonsense", "coordinates": []}, Exception),
        ({"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 1.0]]]}, ValueError),
        ({"coordinates": [[[0.0, 0.0]]]}, AttributeError),
        ("POLYGON((0 0))", AttributeError),
        ({"type": "FeatureCollection", "features": "not a list"}, AttributeError),
    ],
    ids=[
        "no-coordinates",
        "unknown-type",
        "short-ring",
        "no-type",
        "a-string",
        "bad-fc",
    ],
)
def test_polygon_wkt_refuses_input_it_cannot_parse(malformed, original):
    """Malformed GeoJSON is a refusal, not whatever shapely happened to raise.

    Four exception types reach this function depending on how the input is
    wrong, and none of them is something a surface should pass on raw: a CLI
    user would get a traceback and an API client a 500, where every other
    refusal here is a message. They become `InvalidGeometry`, which already
    means this and is already in the CLI's user-error tuple.
    """
    with pytest.raises(InvalidGeometry) as caught:
        polygon_wkt(malformed)

    assert "could not be parsed" in str(caught.value)
    # chained, because shapely's own complaint is the actionable half
    assert isinstance(caught.value.__cause__, original)
    assert str(caught.value.__cause__) in str(caught.value)


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


# ── what a rule can reach ────────────────────────────────────────────────
#
# The scenario workspace offers only these, so the list is a promise to a
# planner: an indicator missing from it is one they cannot ask about, and an
# extra one is a map that never moves however many rules they draw.


def test_a_rule_reaches_exactly_the_five_indicators():
    assert set(reachable_indicators()) == {
        "urban_form_block_planned_floors",
        "edificability_block_housing_units",
        "edificability_block_population",
        "edificability_block_density",
        "livability_block_score",
    }


def test_a_composite_comes_after_the_input_it_reads():
    order = reachable_indicators()
    assert order.index("livability_block_score") > order.index(
        "edificability_block_density"
    )


def test_counts_are_summed_and_rates_are_medians():
    assert table_statistic("units") == "sum"
    assert table_statistic("people") == "sum"
    for unit in ("units_per_ha", "floors", "score_0_1", None):
        assert table_statistic(unit) == "median"


def test_the_allowance_accounts_for_lot_coverage():
    """Density is judged on levels × coverage, so half coverage allows twice
    the storeys; the refusal must name the allowance that would pass."""
    with pytest.raises(CeilingPrecheckFailed) as caught:
        precheck_ceilings(30, 60, 3, 0.5)

    allowed = caught.value.allowed_levels
    assert allowed == math.floor(max_levels_for(60.0) / 0.5)
    precheck_ceilings(allowed, 60, 3, 0.5)  # what it names really passes
    assert f"allows {allowed:d} storeys" in str(caught.value)


def test_a_floors_breach_names_the_floors_bound():
    """Not the combined minimum, and no unit size the user never typed."""
    from reimagina.scenarios.ceilings import max_floors

    levels = max_floors() + 50
    with pytest.raises(CeilingPrecheckFailed) as caught:
        precheck_ceilings(levels, None, None)

    message = str(caught.value)
    assert f"floors ceiling allows {math.floor(max_floors()):d} storeys" in message
    assert "m² per dwelling" not in message


def test_polygon_wkt_repairs_a_self_intersecting_stroke_before_the_union():
    """A bowtie plus a second stroke used to raise GEOS TopologyException in
    the union, which no surface maps to a refusal: HTTP 500."""
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
    }
    square = {
        "type": "Polygon",
        "coordinates": [[[0.2, 0.2], [2, 0.2], [2, 2], [0.2, 2], [0.2, 0.2]]],
    }
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": g, "properties": {}}
            for g in (bowtie, square)
        ],
    }

    assert polygon_wkt(collection).startswith("POLYGON")


# ── "Change by" rules ───────────────────────────────────────────────────────


def test_change_abs_and_pct():
    lv, cov = RULE_INPUTS["levels"], RULE_INPUTS["lot_coverage"]
    assert changed_value(2.0, 1, "abs", lv) == 3.0
    assert changed_value(2.0, 50, "pct", lv) == 3.0
    assert changed_value(0.40, 0.05, "abs", cov) == 0.45
    assert changed_value(0.40, 5, "pct", cov) == 0.42


def test_change_is_clamped_to_the_range():
    assert changed_value(2.0, -3, "abs", RULE_INPUTS["levels"]) == 1.0
    assert changed_value(0.9, 0.5, "abs", RULE_INPUTS["lot_coverage"]) == 1.0
    assert changed_value(14.0, 50, "pct", RULE_INPUTS["people_per_household"]) == 15.0
    assert changed_value(20.0, -50, "pct", RULE_INPUTS["unit_size"]) == 15.0


def test_a_coverage_change_never_leaves_a_block_unbuilt():
    # A set rule refuses 0 % coverage; a change must not reach it either.
    cov = RULE_INPUTS["lot_coverage"]
    assert changed_value(0.40, -0.5, "abs", cov) == 0.0001
    assert changed_value(0.40, -99.99, "pct", cov) == 0.0001


def test_change_inputs_refusals():
    with pytest.raises(ProposalValueError):
        check_inputs({"levels": 0}, units={"levels": "abs"})
    with pytest.raises(ProposalValueError):
        check_inputs({"levels": -100}, units={"levels": "pct"})
    with pytest.raises(ProposalValueError):
        rule_units({"levels": "floors"}, ["levels"])
    with pytest.raises(ProposalValueError):
        rule_units({"storeys": "abs"}, ["levels"])
    with pytest.raises(ProposalValueError):
        rule_units({}, ["levels"], mode="other")
    assert check_inputs({"levels": -2}, units={"levels": "abs"}) == {"levels": -2.0}


def test_each_number_without_a_unit_is_set_unless_an_old_client_says_change():
    # One description of a rule: a unit per number, "set" when none is given.
    assert rule_units({}, ["levels"]) == {"levels": "set"}
    # Clients that still send r058's mode: "change" made bare numbers amounts,
    # "set" made every number a value.
    assert rule_units({}, ["levels"], mode="change") == {"levels": "abs"}
    assert rule_units({"levels": "pct"}, ["levels"], mode="set") == {"levels": "set"}


def test_a_coverage_amount_is_a_share_change_not_points():
    """+5 meant as points would clamp every block to 100 %: refused."""
    with pytest.raises(ProposalValueError):
        check_inputs({"lot_coverage": 5}, units={"lot_coverage": "abs"})
    assert check_inputs({"lot_coverage": 0.05}, units={"lot_coverage": "abs"}) == {
        "lot_coverage": 0.05
    }
    assert check_inputs({"lot_coverage": 5}, units={"lot_coverage": "pct"}) == {
        "lot_coverage": 5.0
    }


# ── One rule, a choice per number ("=", "+ amount", "+ %") ──────────────────


def test_a_rule_can_set_one_number_and_change_another():
    checked = check_inputs(
        {"lot_coverage": 0.4, "levels": 20},
        units={"lot_coverage": "set", "levels": "pct"},
    )
    assert checked == {"levels": 20.0, "lot_coverage": 0.4}


@pytest.mark.parametrize("bad", [0.0, 1.5])
def test_a_set_number_in_a_mixed_rule_keeps_the_set_range(bad):
    with pytest.raises(ProposalValueError):
        check_inputs(
            {"lot_coverage": bad, "levels": 1},
            units={"lot_coverage": "set", "levels": "abs"},
        )


def test_a_set_coverage_may_be_as_low_as_real_blocks_are():
    # 2,662 Mexicali blocks are under 1 % today; a planner may set one.
    assert check_inputs({"lot_coverage": 0.005}) == {"lot_coverage": 0.005}


def test_a_change_never_moves_a_block_the_wrong_way():
    # A floor or ceiling the baseline is already past must not drag the block
    # back across it: a decrease never raises, an increase never lowers.
    cov = RULE_INPUTS["lot_coverage"]
    assert changed_value(0.00005, -20, "pct", cov) == 0.00005
    assert changed_value(0.005, -20, "pct", cov) == 0.004
    assert changed_value(0.005, 10, "pct", cov) == 0.0055
    assert changed_value(12.0, -10, "pct", RULE_INPUTS["unit_size"]) == 12.0
    assert changed_value(16.0, 10, "pct", RULE_INPUTS["people_per_household"]) == 16.0


@pytest.mark.parametrize(
    ("inputs", "units", "plain"),
    [
        (
            {"lot_coverage": 1.2},
            {},
            "Lot coverage must be more than 0 and at most 100 %, got 120 %.",
        ),
        (
            {"levels": 0},
            {"levels": "pct"},
            "Storeys change cannot be 0: leave it empty to keep each block's own "
            "value.",
        ),
        (
            {"lot_coverage": 1.5},
            {"lot_coverage": "abs"},
            "Lot coverage change must be between −100 and +100 points.",
        ),
        ({"levels": -100}, {"levels": "pct"}, "Storeys change must be above −100 %."),
        ({"unit_size": 10}, {}, "m² per home must be more than 15 m², got 10."),
    ],
)
def test_a_refused_number_is_also_said_in_the_planners_units(inputs, units, plain):
    # The page shows this sentence instead of repeating each check itself.
    with pytest.raises(ProposalValueError) as caught:
        check_inputs(inputs, units=units)
    assert caught.value.plain == plain
