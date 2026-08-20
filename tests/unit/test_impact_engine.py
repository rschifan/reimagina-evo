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


def test_independent_units_come_out_in_a_stable_order():
    """The dirty-set order must not depend on set iteration order.

    It used to: the topological sort was fed a `set`, so the result varied with
    PYTHONHASHSEED and this pure function returned a different order in every
    process. Ten processes gave ten distinct orders on a 20-node DAG — each
    topologically valid, which is what made it invisible.

    Two things downstream depend on it. A durable-execution ops-plane replays a
    workflow by matching steps positionally, so a recovering process that
    re-derived a different order fails the replay instead of resuming; and an
    engine that declares the DAG statically builds it from this order at import,
    so two processes could disagree on the workflow they are running.

    Asserting the invariant directly — independent nodes ordered by slug — is
    what makes this checkable in one process.
    """
    seeds = tuple(_spec(f"direct_{i:02d}", reads=("geometry",)) for i in range(6))
    composite = _spec(
        "roll_up",
        kind="composite",
        depends_on=tuple((s.slug, round(1 / 6, 10)) for s in seeds),
    )
    registry = (*seeds, composite)

    order = [u.indicator for u in compute_dirty_set({"geometry"}, {"f1"}, registry)]

    independent = order[:-1]
    assert independent == sorted(
        independent
    ), "independent units must come out sorted, not in set-iteration order"
    assert order[-1] == "roll_up"  # the dependent still comes last


# ── Dataset-level change ─────────────────────────────────────────────────────


def test_a_changed_dataset_seeds_from_its_edges_not_from_reads():
    """"A street was added to the road network" — not an attribute edit.

    The accessibility indicators declare a spatial edge to `global.road_network`
    with no attributes on it, so `reads` cannot see the dependency at all. The
    seed has to come from `inputs`.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import indicators_for_datasets

    hit = indicators_for_datasets(["global.road_network"], SHIPPED_INDICATORS)

    assert "accessibility_block_school_network_m" in hit
    assert "accessibility_block_health_network_m" in hit
    assert "accessibility_block_park_network_m" in hit


def test_a_changed_dataset_propagates_downstream():
    """A composite reading a dirty indicator is dirty too."""
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import indicators_for_datasets

    hit = indicators_for_datasets(["global.building"], SHIPPED_INDICATORS)

    assert "urban_form_block_building_density" in hit
    # composite is derived_from building_density, so it must follow
    assert "urban_form_block_composite" in hit
    assert hit.index("urban_form_block_building_density") < hit.index(
        "urban_form_block_composite"
    )


def test_a_dataset_nothing_reads_recomputes_nothing():
    """A coverage gap, not a fault — and it must not read as one."""
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import indicators_for_datasets

    assert indicators_for_datasets(["mex.lot"], SHIPPED_INDICATORS) == ()
    assert indicators_for_datasets(["not.a.dataset"], SHIPPED_INDICATORS) == ()
