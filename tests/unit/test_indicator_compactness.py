"""Unit tests for reimagina.indicators.compactness.polsby_popper.

The worker itself is exercised by the integration tests; the pure math has
its own unit coverage so regressions in the formula itself fail fast without
requiring a database.
"""

from __future__ import annotations

import math

import pytest

from reimagina.indicators.compactness import polsby_popper


def test_polsby_popper_perfect_circle_is_one():
    # Circle of radius 10 — area = π·100, perimeter = 2π·10 → ratio = 1.
    radius = 10.0
    area = math.pi * radius * radius
    perimeter = 2.0 * math.pi * radius
    assert polsby_popper(area_m2=area, perimeter_m=perimeter) == pytest.approx(1.0)


def test_polsby_popper_unit_square_matches_expected():
    # Square with side 1: area = 1, perimeter = 4 → 4π/16 = π/4 ≈ 0.7854.
    value = polsby_popper(area_m2=1.0, perimeter_m=4.0)
    assert value == pytest.approx(math.pi / 4.0, rel=1e-9)


def test_polsby_popper_thin_rectangle_is_small():
    # 100m × 1m: area = 100, perimeter = 202. Ratio ≈ 4π·100 / 202² ≈ 0.0308.
    value = polsby_popper(area_m2=100.0, perimeter_m=202.0)
    assert value < 0.05
    assert value > 0.0


def test_polsby_popper_degenerate_inputs_return_zero():
    assert polsby_popper(area_m2=0.0, perimeter_m=10.0) == 0.0
    assert polsby_popper(area_m2=-1.0, perimeter_m=10.0) == 0.0
    assert polsby_popper(area_m2=10.0, perimeter_m=0.0) == 0.0


def test_polsby_popper_clamps_floating_point_overshoot_to_one():
    # Inputs that produce ratio slightly > 1 due to float noise; function
    # must clamp so the writer doesn't trip valid_max = 1.
    # Manufacture by hand: 4π·A / P² just above 1.
    area = math.pi
    perimeter = math.sqrt(4.0 * math.pi * area) * (1.0 - 1e-15)
    assert polsby_popper(area_m2=area, perimeter_m=perimeter) == 1.0
