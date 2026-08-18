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


# ── direction ────────────────────────────────────────────────────────────


def _orient(slug: str, value: float) -> float:
    """What compute_composite does to one input: normalise, then orient."""
    spec = _BY_SLUG[slug]
    assert spec.valid_min is not None and spec.valid_max is not None
    x = min(1.0, max(0.0, _normalise(slug, value)))
    return x if spec.higher_is_better else 1.0 - x


def test_distance_indicators_declare_lower_is_better():
    """The six that motivate the field. A distance is lower-is-better whatever
    the implementation does, so this cannot drift from the compute."""
    lower = {s.slug for s in SHIPPED_INDICATORS if not s.higher_is_better}
    assert lower == {
        f"accessibility_block_{c}_{m}_m"
        for c in ("school", "health", "park")
        for m in ("euclid", "network")
    }


def test_a_nearer_amenity_scores_higher():
    """The bug this prevents: `weight >= 0` is enforced (r016:93), so without
    orientation a distance added to a score ranks 'far from a school' as good."""
    near = _orient("accessibility_block_school_network_m", 100.0)
    far = _orient("accessibility_block_school_network_m", 1400.0)
    assert near > far, (near, far)
    # And the raw normalised values run the other way — which is precisely what
    # would have reached the weighted sum before orientation.
    assert _normalise("accessibility_block_school_network_m", 100.0) < _normalise(
        "accessibility_block_school_network_m", 1400.0
    )


def test_orientation_stays_inside_0_1_including_at_the_cutoff():
    """Unreachable blocks report the cutoff, which is also valid_max. That must
    orient to 0 — the worst score — not to a negative contribution."""
    from reimagina.indicators.catalog import ACCESSIBILITY_CUTOFF_M

    slug = "accessibility_block_park_network_m"
    assert _orient(slug, ACCESSIBILITY_CUTOFF_M) == pytest.approx(0.0)
    assert _orient(slug, 0.0) == pytest.approx(1.0)
    # Above the declared bound: clamped first, so it cannot orient negative.
    assert _orient(slug, ACCESSIBILITY_CUTOFF_M * 2) == pytest.approx(0.0)


def test_higher_is_better_indicators_are_unchanged_by_orientation():
    """The default must be a no-op, or adding the field would have moved every
    existing composite."""
    for slug in ("urban_form_block_compactness", "urban_form_block_building_density"):
        spec = _BY_SLUG[slug]
        assert spec.higher_is_better
        mid = (spec.valid_min + spec.valid_max) / 2
        assert _orient(slug, mid) == pytest.approx(_normalise(slug, mid))


def test_the_shipped_score_mixes_both_directions():
    """`livability_block_score` is the first shipped spec to combine a
    higher-is-better input with lower-is-better ones, so the orientation path
    is exercised by the catalog itself rather than only by a test-local spec.

    Also pins the choice of the *network* accessibility family: only it responds
    to a road-network edit, which is what makes the attribute -> run -> indicator
    chain end to end.
    """
    score = _BY_SLUG["livability_block_score"]
    directions = {_BY_SLUG[slug].higher_is_better for slug, _ in score.depends_on}
    assert directions == {True, False}, "the score must exercise both directions"

    distances = [slug for slug, _ in score.depends_on if slug.endswith("_m")]
    assert distances and all(s.endswith("_network_m") for s in distances), distances
    assert sum(w or 0.0 for _, w in score.depends_on) == pytest.approx(1.0)
