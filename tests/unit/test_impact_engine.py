"""Unit tests for the impact engine (dirty-set) — pure, no DB.

Built around the linear-park worked example in
docs/product/PHASE3_ORCHESTRATION_EXPLORATION.md §2.4.
"""

from __future__ import annotations

import graphlib

import pytest

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.impact import DirtyUnit, compute_dirty_set


def _spec(slug, *, reads=(), depends_on=(), kind="direct", applies_to=("block",)):
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind=kind,
        value_type="numeric",
        aggregation_method="weighted_mean",
        applies_to=applies_to,
        reads=reads,
        depends_on=depends_on,
    )


# The §2.4 registry: green_coverage + walkability read land-use/green; the
# composite liveability_index weights walkability/transit/green_coverage; the
# rollup carries liveability_index block→AGEB. transit reads its own attribute.
GREEN = _spec("green_coverage", reads=("land_use", "green_density"))
WALK = _spec("walkability", reads=("land_use", "green_density"))
TRANSIT = _spec("transit", reads=("transit_freq",))
LIVEABILITY = _spec(
    "liveability_index",
    kind="composite",
    depends_on=(("walkability", 0.4), ("transit", 0.3), ("green_coverage", 0.3)),
)
ROLLUP = _spec(
    "liveability_rollup",
    kind="composite",
    depends_on=(("liveability_index", None),),
    applies_to=("ageb",),
)
REGISTRY = (GREEN, WALK, TRANSIT, LIVEABILITY, ROLLUP)


def _by_slug(units: list[DirtyUnit]) -> dict[str, DirtyUnit]:
    return {u.indicator: u for u in units}


def test_linear_park_dirty_set_matches_worked_example():
    units = compute_dirty_set(
        changed_attributes={"land_use", "green_density"},
        edited_features={"b1", "b2", "b3"},
        registry=REGISTRY,
    )
    slugs = [u.indicator for u in units]

    # transit reads a different attribute → never reached.
    assert "transit" not in slugs
    assert set(slugs) == {
        "green_coverage",
        "walkability",
        "liveability_index",
        "liveability_rollup",
    }

    # inputs precede dependents.
    pos = {s: i for i, s in enumerate(slugs)}
    assert pos["green_coverage"] < pos["liveability_index"]
    assert pos["walkability"] < pos["liveability_index"]
    assert pos["liveability_index"] < pos["liveability_rollup"]

    # seed indicators carry the edited features; the composite is the union of
    # its dirty inputs (here both span the same three blocks).
    by = _by_slug(units)
    assert by["green_coverage"].features == frozenset({"b1", "b2", "b3"})
    assert by["liveability_index"].features == frozenset({"b1", "b2", "b3"})


def test_no_changed_attributes_is_empty():
    assert compute_dirty_set(set(), {"b1"}, REGISTRY) == []


def test_unread_attribute_touches_nothing():
    # No indicator declares reading 'owner_name'.
    assert compute_dirty_set({"owner_name"}, {"b1"}, REGISTRY) == []


def test_propagation_from_a_single_seed():
    # transit_freq seeds only transit, which fans out to the composite + rollup.
    slugs = [u.indicator for u in compute_dirty_set({"transit_freq"}, {"b1"}, REGISTRY)]
    assert set(slugs) == {"transit", "liveability_index", "liveability_rollup"}
    assert "green_coverage" not in slugs  # unrelated direct indicator untouched
    pos = {s: i for i, s in enumerate(slugs)}
    assert pos["transit"] < pos["liveability_index"] < pos["liveability_rollup"]


def test_expander_remaps_rollup_to_parent_grain():
    # A rollup recomputes at the parent grain — inject the block→AGEB mapping.
    def expand(spec, feats):
        if "ageb" in spec.applies_to and spec.computation_kind == "composite":
            return frozenset({"ageb_09"})
        return feats

    units = _by_slug(
        compute_dirty_set({"land_use"}, {"b1", "b2", "b3"}, REGISTRY, expand=expand)
    )
    assert units["liveability_rollup"].features == frozenset({"ageb_09"})
    # non-rollup units keep the edited features.
    assert units["green_coverage"].features == frozenset({"b1", "b2", "b3"})


def test_cycle_is_rejected():
    a = _spec("a", reads=("x",), depends_on=(("b", None),))
    b = _spec("b", depends_on=(("a", None),))
    with pytest.raises(graphlib.CycleError):
        compute_dirty_set({"x"}, {"b1"}, (a, b))
