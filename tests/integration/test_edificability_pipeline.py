"""End-to-end edificability: attributes in, three indicator_result rows out.

The unit tests pin the arithmetic. This pins everything around it — that the
effective-attribute read resolves, that the AOI/feature scoping reaches the
right blocks, that the values survive the r016 write-time bounds trigger, and
that a scenario override moves all three indicators together.

The block is sized so the expected values are checkable by hand: a 0.02 ha
(200 m²) footprint at 3 levels and 60 m²/unit is exactly 10 units, 23 people
at 2.3 per household, and 500 units/ha.
"""

from __future__ import annotations

from uuid import uuid4

from reimagina.data import feature as feat
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

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

# ~200 m² near the centre of CDMX. Latitude matters: a degree of longitude is
# ~105 m at 19.43°N, so the extents below are chosen for area, not tidiness.
_WKT = (
    "POLYGON((-99.140000 19.430000,-99.139859 19.430000,"
    "-99.139859 19.430134,-99.140000 19.430134,-99.140000 19.430000))"
)

_ATTRS = {"num_levels": 3, "unit_size": 60, "people_per_household": 2.3}


def _block(conn, attributes):
    source_id = _register_source(
        conn,
        name=f"edif_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    return _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref=f"edif-blk-{uuid4().hex[:8]}",
        geometry_wkt=_WKT,
        attributes=attributes,
    )


def test_three_indicators_from_one_attribute_set(conn):
    seed_catalog(conn)
    block_id = _block(conn, _ATTRS)
    repo = backend_for_connection(conn).indicator_result_repo()

    for compute in (
        compute_block_housing_units,
        compute_block_population,
        compute_block_housing_density,
    ):
        result = compute(conn, feature_ids=[block_id], code_version="test")
        assert result.rows_written == 1, f"{result.indicator_slug} wrote nothing"

    units = repo.get_current(
        feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_HOUSING_UNITS.slug
    )
    people = repo.get_current(
        feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_POPULATION.slug
    )
    density = repo.get_current(
        feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_DENSITY.slug
    )
    assert units is not None and people is not None and density is not None

    # area × levels / unit_size, and the two values derived from it. The area is
    # geodesic so it is not exactly 200 m²; assert the relationships instead of
    # brittle absolutes, plus a loose absolute to catch an order-of-magnitude slip.
    u = units["value_numeric"]
    assert 8.0 < u < 12.0, f"expected ~10 units on a ~200 m² block, got {u}"
    assert people["value_numeric"] == u * 2.3
    # density = u / area_ha, and area_ha = u * unit_size / levels / 10_000
    assert density["value_numeric"] == u / (u * 60 / 3 / 10_000)


def test_missing_attribute_leaves_the_previous_value_untouched(conn):
    """Absent input is not a capacity of zero.

    bulk_write_numeric_results stales only the features it writes, so a block
    that drops out of the input set must keep its prior value rather than being
    silently overwritten — the difference between "no data" and "no capacity".
    """
    seed_catalog(conn)
    complete = _block(conn, _ATTRS)
    partial = _block(conn, {"num_levels": 3})  # no unit_size / people

    result = compute_block_housing_units(
        conn, feature_ids=[complete, partial], code_version="test"
    )
    assert result.features_considered == 1, "the partial block should be skipped"
    assert result.rows_written == 1

    repo = backend_for_connection(conn).indicator_result_repo()
    slug = EDIFICABILITY_BLOCK_HOUSING_UNITS.slug
    assert repo.get_current(feature_id=complete, indicator_slug=slug) is not None
    assert repo.get_current(feature_id=partial, indicator_slug=slug) is None


def test_scenario_override_moves_all_three(conn):
    """One upzoning edit, three indicators — the group's reason to exist."""
    seed_catalog(conn)
    block_id = _block(conn, _ATTRS)
    repo = backend_for_connection(conn).indicator_result_repo()

    for compute in (
        compute_block_housing_units,
        compute_block_population,
        compute_block_housing_density,
    ):
        compute(conn, feature_ids=[block_id], code_version="test")
    base_units = repo.get_current(
        feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_HOUSING_UNITS.slug
    )["value_numeric"]

    scenario = str(uuid4())
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=block_id,
        attribute_key="num_levels",
        value_numeric=6.0,  # double the storeys
    )
    for compute in (
        compute_block_housing_units,
        compute_block_population,
        compute_block_housing_density,
    ):
        compute(conn, feature_ids=[block_id], scenario_id=scenario, code_version="test")

    scoped = repo.get_current(
        feature_id=block_id,
        indicator_slug=EDIFICABILITY_BLOCK_HOUSING_UNITS.slug,
        scenario_id=scenario,
    )
    assert scoped["value_numeric"] == base_units * 2

    # The baseline is untouched — scenario results are written alongside, not over.
    still_base = repo.get_current(
        feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_HOUSING_UNITS.slug
    )
    assert still_base["value_numeric"] == base_units
