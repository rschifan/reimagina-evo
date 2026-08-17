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

from dataclasses import dataclass

from reimagina.data.feature import get_type_id
from reimagina.indicators._hamilton_driver import build_indicator_driver
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
from reimagina.jobs._lifecycle import job_run
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


@dataclass(frozen=True)
class ComputeResult:
    """Outcome of one edificability compute."""

    job_run_id: str
    indicator_slug: str
    features_considered: int
    rows_written: int
    aoi_id: str | None


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
        values = [attributes[key].get(feature_id) for key in _ATTRIBUTES]
        if any(value is None for value in values):
            # Missing input — leave whatever this feature already holds.
            continue
        levels, unit_size, people = values
        rows.append((feature_id, area_m2, levels, unit_size, people))
    return rows


def _compute(
    conn,
    *,
    spec: IndicatorSpec,
    aoi_id: str | None = None,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Read, compute and write one edificability indicator."""
    backend = backend_for_connection(conn)
    if backend.indicator_catalog_repo().get_indicator(spec.slug) is None:
        raise RuntimeError(
            f"Indicator {spec.slug!r} not found in indicator_catalog. "
            "Run `indicators seed-catalog` first."
        )
    block_type_id = get_type_id(conn, "MEX", "block")

    # Read inside the job_run, not before it: indicator_result.job_run_id is a
    # NOT NULL FK (r016), so a read that fails outside the context leaves a
    # failure with no provenance row at all.
    with job_run(
        backend,
        model_family=spec.model_family or "urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        rows = _edificability_rows(
            conn,
            block_type_id=block_type_id,
            aoi_id=aoi_id,
            feature_ids=feature_ids,
            scenario_id=scenario_id,
        )
        node = _FRAME_FOR[spec.slug]
        frame = build_indicator_driver().execute(
            [node], inputs={"edificability_rows": rows}
        )[node]
        rows_written = backend.indicator_result_repo().bulk_write_numeric_results(
            list(frame.itertuples(index=False, name=None)),
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(rows),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )


def compute_block_housing_units(conn, **kwargs) -> ComputeResult:
    """Buildable dwellings per block."""
    return _compute(conn, spec=EDIFICABILITY_BLOCK_HOUSING_UNITS, **kwargs)


def compute_block_population(conn, **kwargs) -> ComputeResult:
    """Residents implied by the buildable dwelling count."""
    return _compute(conn, spec=EDIFICABILITY_BLOCK_POPULATION, **kwargs)


def compute_block_housing_density(conn, **kwargs) -> ComputeResult:
    """Buildable dwellings per hectare."""
    return _compute(conn, spec=EDIFICABILITY_BLOCK_DENSITY, **kwargs)
