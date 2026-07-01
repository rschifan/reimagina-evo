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

from reimagina.indicators.kernels import count_to_density, polsby_popper

_RESULT_COLUMNS = ["feature_id", "value_numeric"]


def compactness_result_frame(
    geodesic_metrics: list[tuple[str, float, float]],
) -> pd.DataFrame:
    """Block Polsby-Popper compactness per feature.

    ``geodesic_metrics`` is ``(feature_id, area_m2, perimeter_m)`` from
    ``FeatureRepository.geodesic_metrics_by_type``.
    """
    rows = [
        (feature_id, polsby_popper(area_m2=area, perimeter_m=perimeter))
        for feature_id, area, perimeter in geodesic_metrics
    ]
    return pd.DataFrame(rows, columns=_RESULT_COLUMNS)


def density_result_frame(
    centroid_counts: list[tuple[str, int, float]],
) -> pd.DataFrame:
    """Block building density per feature.

    ``centroid_counts`` is ``(feature_id, child_count, area_m2)`` from
    ``FeatureRepository.centroid_in_parent_counts``.
    """
    rows = [
        (feature_id, count_to_density(count=count, area_m2=area))
        for feature_id, count, area in centroid_counts
    ]
    return pd.DataFrame(rows, columns=_RESULT_COLUMNS)
