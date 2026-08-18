"""Buildable capacity from construction and land-use attributes.

Three indicators, one calculation — the worked example the Reimagina
collaborators supplied:

    housing_units = (lot_size × num_levels) / unit_size
    population    = housing_units × people_per_household
    density       = housing_units / lot_size_in_hectares

Computed at **block** grain: `lot_size` is the manzana's geodesic area, because
no cadastral lot layer exists yet (`MEX:lot` is a declared feature type with
zero rows). The arithmetic is untouched — only the denominator's grain — so a
real cadastre swaps in by changing the feature type these read.

**Why three public functions over one.** Each is dispatched as its own unit and
must write only its own slug, or `bulk_write_numeric_results` would attribute
one indicator's values to another. `_edificability_rows` is the shared read; the
Hamilton driver computes whichever frames are asked for — so all three repeat
that read rather than sharing it. Measured at 52 ms each on 1,500 blocks, which
is why collapsing them was not worth the dispatch granularity it cost.

⚠ **Features missing any required attribute are skipped, not zeroed.** Absent
data is not a capacity of zero, and `bulk_write_numeric_results` stales only the
features it writes (`feature_id = ANY(...)`), so a skipped feature keeps whatever
value it already had rather than being silently overwritten with 0.
"""

from __future__ import annotations


from reimagina.data.feature import get_type_id
from reimagina.indicators._compute import (
    ComputeResult as ComputeResult,
    compute_indicator,
)
from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    IndicatorSpec,
)

# Re-exported for callers/tests importing the kernels from here; the computes
# below run them through the Hamilton dataflow, not directly.
from reimagina.indicators.kernels import housing_density as housing_density
from reimagina.indicators.kernels import housing_units as housing_units
from reimagina.indicators.kernels import resident_population as resident_population
from reimagina.scenarios.overrides import effective_numeric
from reimagina.storage import backend_for_connection

#: Hamilton node producing each slug's result frame.
_FRAME_FOR: dict[str, str] = {
    EDIFICABILITY_BLOCK_HOUSING_UNITS.slug: "housing_units_result_frame",
    EDIFICABILITY_BLOCK_POPULATION.slug: "population_result_frame",
    EDIFICABILITY_BLOCK_DENSITY.slug: "housing_density_result_frame",
}

#: Attribute keys every edificability indicator needs, in row order.
_ATTRIBUTES = ("num_levels", "unit_size", "people_per_household")


def _edificability_rows(
    conn,
    *,
    block_type_id: str,
    aoi_id: str | None,
    feature_ids: list[str] | None,
    scenario_id: str | None,
) -> list[tuple[str, float, float, float, float]]:
    """``(feature_id, area_m2, num_levels, unit_size, people_per_household)``.

    Area comes from the geodesic metrics query, which is the only one of these
    reads that honours ``aoi_id``; ``effective_numeric`` scopes by feature id
    alone. Driving the join from the area rows therefore applies the AOI filter
    to the result, and the attribute maps are only ever consulted for features
    that survived it.

    Each attribute is the *effective* value — scenario override, then
    observation, then the base jsonb — so a what-if that edits `num_levels`
    moves these indicators without touching base data.
    """
    backend = backend_for_connection(conn)
    areas = backend.feature_repo().geodesic_metrics_by_type(
        type_id=block_type_id,
        aoi_id=aoi_id,
        feature_ids=feature_ids,
    )
    attributes = {
        key: effective_numeric(
            conn,
            type_id=block_type_id,
            attribute_key=key,
            scenario_id=scenario_id,
            feature_ids=feature_ids,
        )
        for key in _ATTRIBUTES
    }

    rows: list[tuple[str, float, float, float, float]] = []
    for feature_id, area_m2, _perimeter_m in areas:
        levels = attributes["num_levels"].get(feature_id)
        unit_size = attributes["unit_size"].get(feature_id)
        people = attributes["people_per_household"].get(feature_id)
        if levels is None or unit_size is None or people is None:
            # Missing input — leave whatever this feature already holds.
            continue
        rows.append((feature_id, area_m2, levels, unit_size, people))
    return rows


def _read(conn, *, aoi_id, feature_ids, scenario_id):
    """All three read identically; only the Hamilton node differs. That is the
    same property that keeps their `reads` tuples identical."""
    return _edificability_rows(
        conn,
        block_type_id=get_type_id(conn, "MEX", "block"),
        aoi_id=aoi_id,
        feature_ids=feature_ids,
        scenario_id=scenario_id,
    )


def _bind(spec: IndicatorSpec):
    return compute_indicator(
        spec,
        reader=_read,
        node=_FRAME_FOR[spec.slug],
        input_key="edificability_rows",
    )


#: Buildable dwellings per block.
compute_block_housing_units = _bind(EDIFICABILITY_BLOCK_HOUSING_UNITS)
#: Residents implied by the buildable dwelling count.
compute_block_population = _bind(EDIFICABILITY_BLOCK_POPULATION)
#: Buildable dwellings per hectare.
compute_block_housing_density = _bind(EDIFICABILITY_BLOCK_DENSITY)
