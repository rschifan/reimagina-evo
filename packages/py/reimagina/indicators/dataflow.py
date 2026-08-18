"""Hamilton dataflow for the shipped indicators (Phase 3, component 4/11/12a).

Each indicator's compute is expressed as pure Hamilton nodes: the DB read is
supplied by the driver as an input (``geodesic_metrics`` / ``centroid_counts``),
the kernels are the existing pure functions from ``reimagina.indicators.kernels``,
and each ``*_result_frame`` node returns a two-column DataFrame
``(feature_id, value_numeric)`` ready for ``bulk_write_numeric_results`` — and,
from Phase 2, carrying a Pandera ``@check_output`` schema.

No I/O or ``job_run`` lifecycle lives here; that stays in the impure compute
wrapper. Keeping the graph pure is what lets Hamilton cache by data-version and
emit clean input→output lineage.
"""

from __future__ import annotations

import pandas as pd
from hamilton.function_modifiers import check_output

from reimagina.indicators.catalog import (
    ACCESSIBILITY_CUTOFF_M,
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
)
from reimagina.indicators.kernels import (
    bounded_distance,
    count_to_density,
    housing_density,
    housing_units,
    polsby_popper,
    resident_population,
)
from reimagina.indicators.validation import bounds_schema

_RESULT_COLUMNS = ["feature_id", "value_numeric"]


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Result rows as the two-column frame bulk_write_numeric_results expects.

    float64 explicitly (not object) so an *empty* result still satisfies the
    bounds schema — an empty object column fails the Pandera dtype check, which
    is how a legitimately empty AOI would otherwise present as a broken run.
    """
    return pd.DataFrame(rows, columns=_RESULT_COLUMNS).astype(
        {"value_numeric": "float64"}
    )


@check_output(
    schema=bounds_schema(
        valid_min=URBAN_FORM_BLOCK_COMPACTNESS.valid_min,
        valid_max=URBAN_FORM_BLOCK_COMPACTNESS.valid_max,
    ),
    importance="fail",
)
def compactness_result_frame(
    geodesic_metrics: list[tuple[str, float, float]],
) -> pd.DataFrame:
    """Block Polsby-Popper compactness per feature.

    ``geodesic_metrics`` is ``(feature_id, area_m2, perimeter_m)`` from
    ``FeatureRepository.geodesic_metrics_by_type``.
    """
    return _frame(
        [
            (feature_id, polsby_popper(area_m2=area, perimeter_m=perimeter))
            for feature_id, area, perimeter in geodesic_metrics
        ]
    )


@check_output(
    schema=bounds_schema(
        valid_min=URBAN_FORM_BLOCK_BUILDING_DENSITY.valid_min,
        valid_max=URBAN_FORM_BLOCK_BUILDING_DENSITY.valid_max,
    ),
    importance="fail",
)
def density_result_frame(
    centroid_counts: list[tuple[str, int, float]],
) -> pd.DataFrame:
    """Block building density per feature.

    ``centroid_counts`` is ``(feature_id, child_count, area_m2)`` from
    ``FeatureRepository.centroid_in_parent_counts``.
    """
    return _frame(
        [
            (feature_id, count_to_density(count=count, area_m2=area))
            for feature_id, count, area in centroid_counts
        ]
    )


# ── Edificability ───────────────────────────────────────────────────────────
#
# All three nodes take the same input — `edificability_rows`, which is
# ``(feature_id, area_m2, num_levels, unit_size, people_per_household)`` — and
# each applies one kernel. Hamilton resolves the shared input once per execute()
# even when several of these are requested together, which is what makes
# computing all three in one pass free relative to computing one.
#
# `housing_units` is recomputed inside the population and density nodes rather
# than taken as an upstream node. Deliberate: a `direct` indicator cannot
# declare `depends_on` — the r016 trigger forces any parent of a dependency edge
# to be `computation_kind='composite'`, and `runners.py` binds every composite
# to the generic weighted sum, which cannot express `× people_per_household`.
# Duplicating one multiplication is cheaper than a migration or than reordering
# a runner branch that `perf/workloads.py` documents as load-bearing.


@check_output(
    schema=bounds_schema(
        valid_min=EDIFICABILITY_BLOCK_HOUSING_UNITS.valid_min,
        valid_max=EDIFICABILITY_BLOCK_HOUSING_UNITS.valid_max,
    ),
    importance="fail",
)
def housing_units_result_frame(
    edificability_rows: list[tuple[str, float, float, float, float]],
) -> pd.DataFrame:
    """Buildable dwellings per block."""
    return _frame(
        [
            (
                feature_id,
                housing_units(area_m2=area, num_levels=levels, unit_size_m2=unit_size),
            )
            for feature_id, area, levels, unit_size, _pph in edificability_rows
        ]
    )


@check_output(
    schema=bounds_schema(
        valid_min=EDIFICABILITY_BLOCK_POPULATION.valid_min,
        valid_max=EDIFICABILITY_BLOCK_POPULATION.valid_max,
    ),
    importance="fail",
)
def population_result_frame(
    edificability_rows: list[tuple[str, float, float, float, float]],
) -> pd.DataFrame:
    """Residents implied by the buildable dwelling count."""
    return _frame(
        [
            (
                feature_id,
                resident_population(
                    housing_units=housing_units(
                        area_m2=area, num_levels=levels, unit_size_m2=unit_size
                    ),
                    people_per_household=pph,
                ),
            )
            for feature_id, area, levels, unit_size, pph in edificability_rows
        ]
    )


@check_output(
    schema=bounds_schema(
        valid_min=EDIFICABILITY_BLOCK_DENSITY.valid_min,
        valid_max=EDIFICABILITY_BLOCK_DENSITY.valid_max,
    ),
    importance="fail",
)
def housing_density_result_frame(
    edificability_rows: list[tuple[str, float, float, float, float]],
) -> pd.DataFrame:
    """Buildable dwellings per hectare.

    Named apart from ``density_result_frame`` (building density) because
    Hamilton addresses nodes by function name — colliding on ``density`` would
    silently redefine the other indicator's node.
    """
    return _frame(
        [
            (
                feature_id,
                housing_density(
                    housing_units=housing_units(
                        area_m2=area, num_levels=levels, unit_size_m2=unit_size
                    ),
                    area_m2=area,
                ),
            )
            for feature_id, area, levels, unit_size, _pph in edificability_rows
        ]
    )


# ── Accessibility ───────────────────────────────────────────────────────────


@check_output(
    schema=bounds_schema(valid_min=0.0, valid_max=ACCESSIBILITY_CUTOFF_M),
    importance="fail",
)
def accessibility_result_frame(
    accessibility_rows: list[tuple[str, float | None]],
) -> pd.DataFrame:
    """Distance-to-nearest per block, clamped into the declared bounds.

    **One node serves all six accessibility indicators.** They differ by
    category and by whether the distance is straight-line or along the network,
    but every one of them is metres in ``[0, ACCESSIBILITY_CUTOFF_M]`` — so six
    nodes would be six copies of one bounds contract, and a divergence between
    them would be a bug rather than a feature. The distinctions live in the SQL
    that produces ``accessibility_rows``, which is where they belong.

    A ``None`` distance means nothing of that category was reachable and reports
    as the cutoff; see ``bounded_distance``.
    """
    return _frame(
        [
            (feature_id, bounded_distance(distance, cutoff_m=ACCESSIBILITY_CUTOFF_M))
            for feature_id, distance in accessibility_rows
        ]
    )
