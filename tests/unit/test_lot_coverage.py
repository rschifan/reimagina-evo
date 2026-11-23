"""Lot coverage: the share of a block that is built on (COS).

A rule carries it as a fifth number. ``housing_units`` becomes
``area × lot_coverage × num_levels / unit_size``, so coverage scales every
area-independent and per-block bound the same way the storeys do.
"""

from __future__ import annotations

import math

import pytest

from reimagina.scenarios.ceilings import BlockInputs, check_ceilings
from reimagina.scenarios.proposal import (
    CeilingPrecheckFailed,
    ProposalValueError,
    check_inputs,
    edits_for,
    precheck_ceilings,
)


def test_edits_for_writes_coverage_last() -> None:
    edits = edits_for(
        levels=3, unit_size=66, people_per_household=3.6, lot_coverage=0.6
    )
    assert [e["attribute_key"] for e in edits] == [
        "floors",
        "num_levels",
        "unit_size",
        "people_per_household",
        "lot_coverage",
    ]
    assert edits[-1]["value_numeric"] == 0.6
    # Left out, coverage is not written: each block keeps its own.
    assert [
        e["attribute_key"]
        for e in edits_for(levels=3, unit_size=66, people_per_household=3.6)
    ][-1] == "people_per_household"


@pytest.mark.parametrize("bad", [0, -0.1, 1.2, math.nan, math.inf])
def test_coverage_must_be_a_share(bad: float) -> None:
    with pytest.raises(ProposalValueError) as exc:
        check_inputs({"lot_coverage": bad})
    assert exc.value.field == "lot_coverage"
    assert "between 0.01 and 1" in str(exc.value)


@pytest.mark.parametrize("good", [0.01, 0.6, 1.0])
def test_a_share_passes(good: float) -> None:
    check_inputs({"lot_coverage": good})


def test_ceilings_scale_by_coverage() -> None:
    # 10000 × 20 / 60 = 3333 units/ha breaches the 2000 density bound at full
    # coverage; at half coverage it is 1666 and passes.
    full = BlockInputs(
        "b", levels=20, unit_size=60, people_per_household=3, area_m2=10_000
    )
    half = BlockInputs(
        "b",
        levels=20,
        unit_size=60,
        people_per_household=3,
        area_m2=10_000,
        lot_coverage=0.5,
    )
    assert any(b.indicator_slug.endswith("density") for b in check_ceilings([full]))
    assert check_ceilings([half]) == []


def test_precheck_uses_coverage() -> None:
    with pytest.raises(CeilingPrecheckFailed):
        precheck_ceilings(20, 60, 3)
    precheck_ceilings(20, 60, 3, 0.5)


def test_the_cli_names_a_refused_coverage_by_its_flag() -> None:
    from apps.cli.scenario import _flags
    from reimagina.scenarios.proposal import check_inputs

    with pytest.raises(
        ValueError, match="--lot-coverage must be a share between 0.01 and 1"
    ):
        with _flags():
            check_inputs({"levels": 3, "unit_size": 66, "lot_coverage": 1.5})
