"""Lot coverage through the real edificability read.

``housing_units = area × lot_coverage × num_levels / unit_size``; a block
with no ``lot_coverage`` is computed at 1.0, so the census-fitted baseline
does not move.
"""

from __future__ import annotations
from tests.integration._helpers import make_scenario


import pytest

from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    seed_catalog,
)
from reimagina.indicators.edificability import (
    compute_block_housing_density,
    compute_block_housing_units,
    compute_block_population,
)
from reimagina.scenarios.overrides import apply_override
from reimagina.storage import backend_for_connection

from tests.integration.test_edificability_pipeline import _ATTRS, _block

_COMPUTES = (
    compute_block_housing_units,
    compute_block_population,
    compute_block_housing_density,
)
_SLUGS = (
    EDIFICABILITY_BLOCK_HOUSING_UNITS.slug,
    EDIFICABILITY_BLOCK_POPULATION.slug,
    EDIFICABILITY_BLOCK_DENSITY.slug,
)


def _values(conn, block_id, scenario_id=None) -> dict[str, float]:
    for compute in _COMPUTES:
        compute(
            conn, feature_ids=[block_id], scenario_id=scenario_id, code_version="test"
        )
    repo = backend_for_connection(conn).indicator_result_repo()
    return {
        slug: repo.get_current(
            feature_id=block_id, indicator_slug=slug, scenario_id=scenario_id
        )["value_numeric"]
        for slug in _SLUGS
    }


def test_absent_coverage_is_one(conn):
    seed_catalog(conn)
    bare = _block(conn, _ATTRS)
    full = _block(conn, {**_ATTRS, "lot_coverage": 1.0})
    assert _values(conn, bare) == pytest.approx(_values(conn, full))


def test_rule_coverage_scales_units_population_and_density(conn):
    seed_catalog(conn)
    block_id = _block(conn, _ATTRS)
    baseline = _values(conn, block_id)

    scenario = make_scenario(conn)
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=block_id,
        attribute_key="lot_coverage",
        value_numeric=0.6,
    )
    scoped = _values(conn, block_id, scenario)
    for slug in _SLUGS:
        assert scoped[slug] == pytest.approx(0.6 * baseline[slug]), slug
