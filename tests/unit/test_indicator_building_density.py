"""Unit tests for reimagina.indicators.building_density.count_to_density."""
from __future__ import annotations

import pytest

from reimagina.indicators.building_density import count_to_density


def test_one_hectare_ten_buildings_is_ten_per_ha():
    assert count_to_density(count=10, area_m2=10_000.0) == pytest.approx(10.0)


def test_half_hectare_five_buildings_is_ten_per_ha():
    assert count_to_density(count=5, area_m2=5_000.0) == pytest.approx(10.0)


def test_zero_count_is_zero_density():
    assert count_to_density(count=0, area_m2=12_345.0) == 0.0


def test_zero_area_returns_zero_density():
    # Blocks with malformed / empty geometry must not crash the worker; they
    # get a 0.0 row rather than propagating a division error.
    assert count_to_density(count=5, area_m2=0.0) == 0.0


def test_negative_area_returns_zero_density():
    assert count_to_density(count=5, area_m2=-1.0) == 0.0


def test_negative_count_returns_zero_density():
    # Shouldn't happen in practice — belt-and-braces check so bad upstream
    # data surfaces as 0 rather than a negative density that would trip the
    # valid_min = 0 bound in the indicator catalog.
    assert count_to_density(count=-2, area_m2=10_000.0) == 0.0


def test_mega_dense_below_valid_max():
    # Real dense MX urban blocks top out ≈ 200/ha; ensure computing 200/ha
    # stays inside the catalog's valid_max = 500.
    value = count_to_density(count=200, area_m2=10_000.0)
    assert value == pytest.approx(200.0)
    assert value <= 500.0
