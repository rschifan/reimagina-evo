"""The three ceilings a scenario's attribute values can breach."""

from __future__ import annotations

import pytest

from reimagina.scenarios import ceilings
from reimagina.scenarios.ceilings import BlockInputs, check_ceilings, max_levels_for

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
