"""Guards on the shipped indicator registry itself.

These are cheap invariants with expensive failure modes: a composite whose
weights do not sum to 1.0 trips a `RAISE EXCEPTION` in the indicator_dependency
trigger, and `bootstrap_local_platform` seeds the catalog, so a bad entry breaks
`make migrate` for everyone rather than failing in one test.
"""

from __future__ import annotations

import itertools

import pytest

from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.orchestration.runners import default_runners

_COMPOSITES = [s for s in SHIPPED_INDICATORS if s.computation_kind == "composite"]

_RUN_GROUPS = {
    group: list(members)
    for group, members in itertools.groupby(
        sorted(
            (s for s in SHIPPED_INDICATORS if s.run_group),
            key=lambda s: s.run_group or "",
        ),
        key=lambda s: s.run_group or "",
    )
}


def test_registry_ships_at_least_one_composite():
    """Without a composite the dirty-set is always one wave.

    Dependency ordering, downstream propagation and the skip-a-dependent-of-a-
    failed-input rule then have no production exercise at all.
    """
    assert _COMPOSITES, "the registry must ship a composite to exercise the DAG"


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_weights_sum_to_one(spec):
    # indicator_dependency's trigger raises outside a 1e-9 tolerance.
    total = sum(weight or 0.0 for _dep, weight in spec.depends_on)
    assert abs(total - 1.0) < 1e-9, f"{spec.slug} weights sum to {total}, not 1.0"


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_has_no_model_family(spec):
    # indicator_catalog CHECK: composites must have model_family NULL.
    assert spec.model_family is None


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_inputs_are_registered_before_it(spec):
    """seed_catalog iterates in order and set_dependencies needs parents first."""
    order = [s.slug for s in SHIPPED_INDICATORS]
    for dep_slug, _weight in spec.depends_on:
        assert dep_slug in order, f"{spec.slug} depends on unregistered {dep_slug}"
        assert order.index(dep_slug) < order.index(spec.slug)


def test_every_shipped_indicator_has_a_runner():
    """The CLI and the coordinator both dispatch through this registry."""
    runners = default_runners()
    missing = [s.slug for s in SHIPPED_INDICATORS if s.slug not in runners]
    assert not missing, f"no runner registered for: {missing}"


@pytest.mark.parametrize("group", sorted(_RUN_GROUPS), ids=lambda g: g)
def test_run_group_members_share_inputs(group):
    """A run_group must be dirty all-or-nothing, or its child runs twice.

    The wave scheduler works on (slug, status) pairs and knows nothing about
    groups. Members with differing `reads` or `depends_on` can therefore become
    ready in *different* waves, and the parent would enqueue the group's child
    once per wave — running one expensive body twice. `s_compute` sets
    retries_allowed=False precisely because a second execution is never
    acceptable, and tests/workloads asserts duplicate_executions is empty on
    every engine.

    Identical inputs make that structurally impossible, which is cheaper than
    teaching the scheduler about groups.
    """
    members = _RUN_GROUPS[group]
    reads = {s.slug: tuple(sorted(s.reads)) for s in members}
    deps = {s.slug: tuple(sorted(s.depends_on)) for s in members}
    assert (
        len(set(reads.values())) == 1
    ), f"run_group {group!r} has mixed reads: {reads}"
    assert (
        len(set(deps.values())) == 1
    ), f"run_group {group!r} has mixed depends_on: {deps}"
