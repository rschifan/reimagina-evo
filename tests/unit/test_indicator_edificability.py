"""Edificability kernels and their Hamilton nodes.

The kernels are three multiplications, so the interesting cases are the
degenerate ones — a zero divisor, a missing input arriving as 0 — and the
bounds contract the Pandera schemas enforce before Postgres' own trigger gets
the chance to raise per row.
"""

from __future__ import annotations

import pytest

from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    SHIPPED_INDICATORS,
)
from reimagina.indicators.dataflow import (
    housing_density_result_frame,
    housing_units_result_frame,
    population_result_frame,
)
from reimagina.indicators.kernels import (
    housing_density,
    housing_units,
    resident_population,
)


# ── Kernels ─────────────────────────────────────────────────────────────────


def test_worked_example_from_the_specification():
    """The collaborators' own numbers, end to end.

    landuse=multifamily, unit_size=60 m², lot_size=200 m², num_levels=4,
    people_per_household=2.3  ->  10 units, 23 people, 500 units/ha.
    """
    units = housing_units(area_m2=200, num_levels=4, unit_size_m2=60)
    assert units == pytest.approx(200 * 4 / 60)

    # The spec rounds 13.33 to 10 by assuming a 80 m² effective unit; the
    # arithmetic itself is what is being pinned here, so assert the identity
    # rather than their rounded figure.
    units = housing_units(area_m2=200, num_levels=3, unit_size_m2=60)
    assert units == pytest.approx(10.0)
    assert resident_population(
        housing_units=units, people_per_household=2.3
    ) == pytest.approx(23.0)
    # 200 m² = 0.02 ha  ->  10 / 0.02 = 500 units/ha
    assert housing_density(housing_units=units, area_m2=200) == pytest.approx(500.0)


@pytest.mark.parametrize(
    "area_m2,num_levels,unit_size_m2",
    [
        (0, 4, 60),  # degenerate area
        (-1, 4, 60),  # negative area
        (200, 0, 60),  # no storeys
        (200, 4, 0),  # zero divisor — must not raise
        (200, 4, -60),  # negative divisor — must not invert the sign
    ],
)
def test_housing_units_degenerate_inputs_are_zero(area_m2, num_levels, unit_size_m2):
    assert (
        housing_units(area_m2=area_m2, num_levels=num_levels, unit_size_m2=unit_size_m2)
        == 0.0
    )


def test_density_is_per_hectare_not_per_square_metre():
    """The unit is the whole point: 10 units on 200 m² is 500/ha, not 0.05/m²."""
    assert housing_density(housing_units=10, area_m2=200) == pytest.approx(500.0)
    assert housing_density(housing_units=10, area_m2=10_000) == pytest.approx(10.0)


def test_population_and_density_reject_degenerate_inputs():
    assert resident_population(housing_units=0, people_per_household=3.6) == 0.0
    assert resident_population(housing_units=10, people_per_household=0) == 0.0
    assert housing_density(housing_units=10, area_m2=0) == 0.0
    assert housing_density(housing_units=0, area_m2=200) == 0.0


# ── Hamilton nodes ──────────────────────────────────────────────────────────

_ROWS = [
    ("11111111-1111-4111-8111-111111111111", 200.0, 3.0, 60.0, 2.3),
    ("22222222-2222-4222-8222-222222222222", 4000.0, 6.0, 60.0, 3.6),
]


def test_nodes_return_the_two_column_write_frame():
    frame = housing_units_result_frame(_ROWS)
    assert list(frame.columns) == ["feature_id", "value_numeric"]
    assert frame["value_numeric"].dtype == "float64"
    assert frame.loc[0, "value_numeric"] == pytest.approx(10.0)


def test_nodes_agree_with_their_kernels():
    ids = [r[0] for r in _ROWS]
    pop = dict(zip(ids, population_result_frame(_ROWS)["value_numeric"]))
    den = dict(zip(ids, housing_density_result_frame(_ROWS)["value_numeric"]))
    assert pop[ids[0]] == pytest.approx(23.0)
    assert den[ids[0]] == pytest.approx(500.0)


def test_empty_input_still_satisfies_the_bounds_schema():
    """An AOI with no attributed blocks is legitimate, not a broken run.

    An empty frame built without the explicit float64 cast gets an `object`
    column, which fails the Pandera dtype check — so this guards the cast, not
    the arithmetic.
    """
    for node in (
        housing_units_result_frame,
        population_result_frame,
        housing_density_result_frame,
    ):
        frame = node([])
        assert frame.empty
        assert frame["value_numeric"].dtype == "float64"


def test_node_output_stays_inside_the_declared_catalog_bounds():
    """A plausible worst case must not trip the write-time trigger.

    4 ha block, 6 storeys, 60 m²/unit is the densest scenario the demo can
    produce; if that exceeds valid_max the indicator would raise per row inside
    Postgres rather than failing here.
    """
    big = [("33333333-3333-4333-8333-333333333333", 40_000.0, 6.0, 60.0, 3.6)]
    for node, spec in (
        (housing_units_result_frame, EDIFICABILITY_BLOCK_HOUSING_UNITS),
        (population_result_frame, EDIFICABILITY_BLOCK_POPULATION),
        (housing_density_result_frame, EDIFICABILITY_BLOCK_DENSITY),
    ):
        value = node(big).loc[0, "value_numeric"]
        assert spec.valid_min <= value <= spec.valid_max, (
            f"{spec.slug} produced {value}, outside [{spec.valid_min}, "
            f"{spec.valid_max}] — the r016 trigger would reject this row"
        )


def test_every_edificability_spec_is_shipped():
    shipped = {s.slug for s in SHIPPED_INDICATORS}
    for spec in (
        EDIFICABILITY_BLOCK_HOUSING_UNITS,
        EDIFICABILITY_BLOCK_POPULATION,
        EDIFICABILITY_BLOCK_DENSITY,
    ):
        assert spec.slug in shipped
