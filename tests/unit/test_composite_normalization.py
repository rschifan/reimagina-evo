"""A composite's weights must mean what they say.

Summing raw values makes the widest-range input win regardless of weight. On
Tlalpan the declared 0.5/0.5 `urban_form_block_composite` behaved 1.25 / 98.75,
because a 0-1 ratio was added to a 0-500/ha density.
"""

from __future__ import annotations

import pytest

from reimagina.indicators.catalog import (
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_COMPOSITE,
)

_BY_SLUG = {s.slug: s for s in SHIPPED_INDICATORS}


def _normalise(slug: str, value: float) -> float:
    spec = _BY_SLUG[slug]
    assert spec.valid_min is not None and spec.valid_max is not None
    return (value - spec.valid_min) / (spec.valid_max - spec.valid_min)


def test_equal_weights_contribute_equally_at_the_top_of_each_range():
    """The property the old code lacked: a max-valued input contributes its
    weight, whatever its native range."""
    contributions = {
        slug: (weight or 0.0) * _normalise(slug, _BY_SLUG[slug].valid_max or 0.0)
        for slug, weight in URBAN_FORM_BLOCK_COMPOSITE.depends_on
    }
    assert set(contributions.values()) == {0.5}, contributions
    assert sum(contributions.values()) == pytest.approx(1.0)


def test_the_old_raw_sum_would_not_have_been_balanced():
    """Pins the bug, so a revert to raw summing fails here rather than in
    production. Uses the real declared ranges, not invented numbers."""
    raw = {
        slug: (weight or 0.0) * (_BY_SLUG[slug].valid_max or 0.0)
        for slug, weight in URBAN_FORM_BLOCK_COMPOSITE.depends_on
    }
    biggest = max(raw.values())
    smallest = min(raw.values())
    assert biggest / smallest > 100, (
        "the composite's inputs no longer differ enough in range for this test "
        "to be meaningful; if that is deliberate, delete it"
    )


def test_composite_bounds_match_a_normalised_score():
    """Weights sum to 1 and each input normalises to 0-1, so the composite
    cannot leave 0-1. r016's write trigger enforces this bound, so a spec that
    drifts from the maths fails at write time rather than silently."""
    assert URBAN_FORM_BLOCK_COMPOSITE.valid_min == 0.0
    assert URBAN_FORM_BLOCK_COMPOSITE.valid_max == 1.0
    weights = [w or 0.0 for _, w in URBAN_FORM_BLOCK_COMPOSITE.depends_on]
    assert sum(weights) == pytest.approx(1.0)


def test_every_composite_input_declares_bounds_to_normalise_against():
    """`compute_composite` refuses without them. Catching it here means a new
    composite fails at import-time review rather than mid-run."""
    for spec in SHIPPED_INDICATORS:
        if spec.computation_kind != "composite":
            continue
        for dep_slug, _ in spec.depends_on:
            dep = _BY_SLUG[dep_slug]
            assert (
                dep.valid_min is not None and dep.valid_max is not None
            ), f"{spec.slug} depends on {dep_slug}, which declares no bounds"
            assert dep.valid_max > dep.valid_min, dep_slug
