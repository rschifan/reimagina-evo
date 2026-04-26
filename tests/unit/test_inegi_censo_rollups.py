"""Unit tests for reimagina.pipeline.inegi_censo_rollups spec coherence.

DB-free. The seed + run round-trip against real Postgres + PostGIS is
covered by the integration suite.
"""
from __future__ import annotations

from reimagina.pipeline.inegi_censo import HEADLINE_COLUMNS
from reimagina.pipeline.inegi_censo_rollups import (
    INPUT_FEATURE_TYPE_SLUG,
    OVERLAP_BASIS,
    ROLLUP_METHOD,
    TARGET_GRAINS,
    RollupSpec,
    specs,
)


def test_specs_cover_every_column_and_grain():
    """7 headline columns × 2 target grains = 14 specs."""
    out = specs()
    assert len(out) == len(HEADLINE_COLUMNS) * len(TARGET_GRAINS)


def test_specs_carry_canonical_attribute_keys():
    """Output keys must be ``censo_2010_<col>_<grain>`` so the picker
    can disambiguate the same demographic at different grains."""
    spec = RollupSpec(column="pobtot", target_feature_type_slug="ageb")
    assert spec.input_attribute_key_prefix == "censo_2010_pobtot"
    assert spec.output_attribute_key_prefix == "censo_2010_pobtot_ageb"

    spec_muni = RollupSpec(column="pobtot", target_feature_type_slug="municipality")
    assert spec_muni.output_attribute_key_prefix == "censo_2010_pobtot_municipality"


def test_specs_input_feature_type_is_block():
    """Census ingest writes block-grain rows, so every rollup sources
    from the same input grain. Pinning here means a future change to
    target a different input grain has to update tests + this constant
    together."""
    assert INPUT_FEATURE_TYPE_SLUG == "block"


def test_rollup_method_is_sum():
    """Population + housing variables are extensive — sum aggregates
    correctly. Mean would be wrong (would average per-block counts and
    lose the absolute population figure)."""
    assert ROLLUP_METHOD == "sum"


def test_overlap_basis_is_area():
    """Block→AGEB / block→municipality overlaps use area-weighted
    aggregation by default, since blocks have a meaningful area
    measure (unlike point features whose only basis would be presence)."""
    assert OVERLAP_BASIS == "area"


def test_target_grains_are_strict_supersets_of_block():
    """AGEB groups several blocks; municipality groups several AGEBs.
    Both are strictly coarser than block — that's what makes the
    rollup well-defined. If a future grain (e.g. neighborhood) joins
    the list, this test forces a deliberate update."""
    assert set(TARGET_GRAINS) == {"ageb", "municipality"}


def test_specs_explicit_columns_subset():
    """Caller can ask for a strict column subset — useful for staged
    rollouts or tests that don't need all 7 columns."""
    out = specs(columns=("pobtot", "vivtot"), target_grains=("ageb",))
    assert len(out) == 2
    assert {s.column for s in out} == {"pobtot", "vivtot"}
    assert {s.target_feature_type_slug for s in out} == {"ageb"}


def test_specs_are_immutable():
    import dataclasses
    spec = RollupSpec(column="pobtot", target_feature_type_slug="ageb")
    try:
        spec.column = "vivtot"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("RollupSpec should be a frozen dataclass")
