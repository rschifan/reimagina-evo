"""The fitted baseline gives each block's census dwellings and population back.

Each scenario input is fitted from real data (coverage and storeys from the
buildings, m² per dwelling and people per household from the census), so when
a scenario's inputs equal the baseline's, the edificability formula must give
the census count, not 14% more people or a block built over wall to wall.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reimagina.indicators.kernels import housing_units, resident_population

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from seed_demo_attributes import MIN_UNIT_SIZE_M2, fit_payloads  # noqa: E402

#: (id, area m², footprint m², dwellings, population, storeys observation)
BLOCKS = [
    ("tagged", 10_000.0, 4_000.0, 40.0, 110.0, 1.37),
    ("one-storey", 8_000.0, 2_400.0, 20.0, 52.0, None),
    ("overbuilt", 1_000.0, 1_300.0, 9.0, 30.0, 2.0),
    ("unmapped", 5_000.0, 0.0, 12.0, 41.0, None),
    ("all-empty", 2_000.0, 500.0, 6.0, 0.0, None),
    ("park", 30_000.0, 100.0, 0.0, 0.0, None),
    ("no-census", 3_000.0, 900.0, None, None, None),
    # Overture maps a sliver: 100 m² ÷ 23 dwellings = 4.3 m² per dwelling.
    ("thin", 2_000.0, 100.0, 23.0, 64.0, None),
]


def _formula(p, area):
    """edificability.py's composition: CUS = coverage × levels."""
    units = housing_units(
        area_m2=area,
        num_levels=p["num_levels"] * p["lot_coverage"],
        unit_size_m2=p["unit_size"],
    )
    return units, resident_population(
        housing_units=units, people_per_household=p["people_per_household"]
    )


def test_the_baseline_inputs_give_the_census_dwellings_and_population():
    fitted = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    for fid, area, _fp, dwellings, population, _s in BLOCKS:
        if not dwellings:
            continue
        units, people = _formula(fitted[fid], area)
        assert units == pytest.approx(dwellings, rel=1e-12), fid
        assert people == pytest.approx(population, rel=1e-12, abs=1e-9), fid


def test_the_inputs_are_physical_quantities():
    fitted = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    tagged = fitted["tagged"]
    assert tagged["lot_coverage"] == 0.4
    assert tagged["num_levels"] == tagged["floors"] == 1.37
    # Built floor area over dwellings: 4,000 m² × 1.37 ÷ 40.
    assert tagged["unit_size"] == pytest.approx(137.0)
    assert tagged["people_per_household"] == pytest.approx(110 / 40)
    # No tagged building: one storey. Coverage never above the whole block.
    assert fitted["one-storey"]["num_levels"] == 1.0
    assert fitted["overbuilt"]["lot_coverage"] == 1.0


def test_a_block_without_census_dwellings_is_skipped_not_built_on():
    fitted = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    for fid in ("park", "no-census"):
        assert "unit_size" not in fitted[fid]
        # A rule that sets a dwelling size there still has a population.
        assert fitted[fid]["people_per_household"] == round(
            (110 + 52 + 30 + 41 + 0 + 64) / (40 + 20 + 9 + 12 + 6 + 23), 4
        )


def test_dwellings_without_a_mapped_footprint_take_the_median_coverage():
    fitted = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    # Covered blocks with dwellings, the thin one left out: 0.25, 0.3, 0.4,
    # 1.0 -> upper median 0.4.
    assert fitted["unmapped"]["lot_coverage"] == 0.4


def test_a_dwelling_size_under_the_floor_takes_the_median_coverage():
    """A footprint so thin it leaves under 20 m² per dwelling is a mapping gap,
    not a block of cupboards: it is treated like an unmapped block."""
    assert MIN_UNIT_SIZE_M2 == 20.0
    thin = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}["thin"]
    assert thin["lot_coverage"] == 0.4
    assert thin["unit_size"] == pytest.approx(2_000 * 0.4 / 23)


def test_estimated_blocks_are_flagged_with_the_reason():
    fitted = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    assert fitted["unmapped"]["estimated"] == "no_footprint"
    assert fitted["thin"]["estimated"] == "thin_footprint"
    flagged = {fid for fid, p in fitted.items() if "estimated" in p}
    assert flagged == {"unmapped", "thin"}


def test_the_fit_is_deterministic_whatever_the_block_order():
    forward = {p["feature_id"]: p for p in fit_payloads(BLOCKS)}
    backward = {p["feature_id"]: p for p in fit_payloads(BLOCKS[::-1])}
    assert forward == backward
