"""Polsby-Popper block compactness: pure math + compute worker.

Reference: Polsby, D. D., & Popper, R. D. (1991). "The Third Criterion:
Compactness as a Procedural Safeguard Against Partisan Gerrymandering." The
shape's area divided by the area of a circle with the same perimeter:

    compactness = 4π × area / perimeter²

    0 → arbitrarily jagged / elongated
    1 → perfect circle (the most compact shape possible for a given perimeter)

Urban context: low scores tend to correlate with historical / irregular
street patterns (e.g. Cuauhtémoc's colonial centro), high scores with
planned grids (most post-1950 Mexican block developments).
"""

from __future__ import annotations

from reimagina.data.feature import get_type_id
from reimagina.indicators._compute import (
    ComputeResult as ComputeResult,
    compute_indicator,
)
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_COMPACTNESS

# Re-exported for callers/tests that import the kernel from here; the compute
# below runs it through the Hamilton dataflow, not directly.
from reimagina.indicators.kernels import polsby_popper as polsby_popper
from reimagina.storage import backend_for_connection


def _compactness_rows(conn, *, aoi_id, feature_ids, scenario_id):
    """Geodesic area and perimeter per block. Geometry-derived, so no scenario."""
    return (
        backend_for_connection(conn)
        .feature_repo()
        .geodesic_metrics_by_type(
            type_id=get_type_id(conn, "MEX", "block"),
            aoi_id=aoi_id,
            feature_ids=feature_ids,
        )
    )


#: Compute Polsby-Popper compactness for every MEX:block feature. Scope with
#: `aoi_id` to limit the run to blocks intersecting an AOI (e.g. one alcaldía).
compute_block_compactness = compute_indicator(
    URBAN_FORM_BLOCK_COMPACTNESS,
    reader=_compactness_rows,
    node="compactness_result_frame",
    input_key="geodesic_metrics",
)
