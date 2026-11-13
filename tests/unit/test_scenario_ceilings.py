"""The four ceilings a scenario's attribute values can breach."""

from __future__ import annotations

import pytest

from reimagina.scenarios import ceilings
from reimagina.scenarios.ceilings import (
    DENSITY,
    FLOORS,
    BlockInputs,
    check_ceilings,
    max_floors,
    max_levels_for,
)

SMALL = 5_944  # median Tlalpan manzana
BIG = 138_695 * 60 / 6  # the area the catalog's measured 138,695-unit block has


def _block(fid="b1", *, levels, unit_size=60, pph=3.6, area=SMALL):
    return BlockInputs(
        feature_id=fid,
        levels=levels,
        unit_size=unit_size,
        people_per_household=pph,
        area_m2=area,
    )


def test_a_block_within_every_bound_produces_no_breach():
    assert check_ceilings([_block(levels=6)]) == []
    assert max_levels_for(60.0) == pytest.approx(12.0)  # 10000*L/60 <= 2000


def test_a_density_breach_names_the_block_the_bound_and_the_value():
    [breach] = check_ceilings([_block("b1", levels=13)])
    assert breach.feature_id == "b1"
    assert breach.indicator_slug == "edificability_block_density"
    assert breach.bound == 2000.0
    assert breach.would_be == pytest.approx(10000 * 13 / 60)


def test_the_housing_units_bound_depends_on_the_block_s_own_area():
    assert check_ceilings([_block(levels=6, area=BIG)]) == []
    # pph=2 keeps population (277,390 * 2) under its own bound, so only the
    # units bound is in play; at the default 3.6 population breaches too.
    breaches = check_ceilings([_block(levels=12, pph=2, area=BIG)])
    assert {b.indicator_slug for b in breaches} == {"edificability_block_housing_units"}


def test_the_population_bound_also_scales_with_household_size():
    assert any(
        b.indicator_slug == "edificability_block_population"
        for b in check_ceilings([_block(levels=6, pph=40, area=BIG)])
    )


def test_blocks_are_judged_separately_not_blended():
    breaches = check_ceilings(
        [
            _block("small_ok", levels=6, area=SMALL),
            _block("big_breach", levels=12, area=BIG),
        ]
    )
    assert {b.feature_id for b in breaches} == {"big_breach"}


def test_every_breaching_block_is_reported_not_just_the_first():
    breaches = check_ceilings([_block("b1", levels=13), _block("b2", levels=13)])
    assert {b.feature_id for b in breaches} == {"b1", "b2"}


def test_the_bounds_are_read_from_the_catalog_at_call_time(monkeypatch):
    # Patchable only because _bound_for looks the slug up per call. If the
    # bound were captured at import, max_levels_for would not move and this
    # would fail for a reason unrelated to the behaviour being pinned.
    monkeypatch.setattr(
        ceilings,
        "_bound_for",
        lambda slug: 1000.0 if slug.endswith("_density") else 1e12,
    )
    assert max_levels_for(60.0) == pytest.approx(6.0)


def test_a_floors_breach_is_found_though_density_is_legal():
    levels = int(max_floors()) + 1
    unit_size = 1.1 * levels / max_levels_for(1.0)  # density comfortably legal
    assert max_levels_for(unit_size) > levels  # precondition: only floors in play
    [breach] = check_ceilings(
        [_block("b1", levels=levels, unit_size=unit_size, area=1)]
    )
    assert breach.indicator_slug == FLOORS
    assert breach.bound == max_floors()
    assert breach.would_be == levels
    assert (
        check_ceilings([_block(levels=int(max_floors()), unit_size=unit_size, area=1)])
        == []
    )


def test_two_breaches_on_one_block_are_both_reported():
    # 13 storeys at 60 m2 breaches density; the floors bound is breached too
    # once levels pass it, and the block must yield an entry for each.
    levels = max_floors() + 1
    breaches = check_ceilings([_block("b1", levels=levels, unit_size=60, area=1)])
    assert [b.feature_id for b in breaches] == ["b1", "b1"]
    assert {b.indicator_slug for b in breaches} == {FLOORS, DENSITY}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # non-positive levels: skipped outright, floors included
        ({"levels": 0, "unit_size": 60}, set()),
        ({"levels": -300, "unit_size": 60}, set()),
        # non-positive unit size skips the unit-size-dependent bounds, but
        # floors does not depend on it, so an absurd storey count still breaches
        ({"levels": 6, "unit_size": 0}, set()),
        ({"levels": 250, "unit_size": 0}, {FLOORS}),
        # zero area: units and population are 0, but density is judged with the
        # area cancelled, as the proposal precheck relies on (documented)
        ({"levels": 6, "unit_size": 60, "area": 0}, set()),
        ({"levels": 13, "unit_size": 60, "area": 0}, {DENSITY}),
    ],
)
def test_degenerate_inputs_breach_only_what_the_docstring_says(kwargs, expected):
    breaches = check_ceilings([_block(**kwargs)])
    assert {b.indicator_slug for b in breaches} == expected
