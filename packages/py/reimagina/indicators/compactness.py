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

import math
from dataclasses import dataclass

from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_COMPACTNESS
from reimagina.storage import backend_for_connection


def polsby_popper(*, area_m2: float, perimeter_m: float) -> float:
    """Compactness on [0, 1]. Returns 0 for degenerate shapes.

    Pure function so it can be unit-tested and reused outside the worker.
    Clamps to 1.0 on the top end to absorb floating-point noise that would
    otherwise trip the valid_max = 1 bound in the catalog.
    """
    if area_m2 <= 0 or perimeter_m <= 0:
        return 0.0
    value = (4.0 * math.pi * area_m2) / (perimeter_m * perimeter_m)
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


@dataclass(frozen=True)
class ComputeResult:
    """Outcome of a compute_block_compactness run."""

    job_run_id: str
    indicator_slug: str
    features_considered: int
    rows_written: int
    aoi_id: str | None


def compute_block_compactness(
    conn,
    *,
    aoi_id: str | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Compute Polsby-Popper compactness for every MEX:block feature.

    Scope with `aoi_id` to limit the run to blocks intersecting an AOI (e.g.
    one alcaldía), matching the pattern established by the overlap worker.
    Creates a job_run row, writes the per-block indicator_result rows through
    bulk_write_numeric_results, and completes the job.

    Returns a `ComputeResult` describing the run. Callers that want to
    inspect individual rows go through serving_geo.current_indicator_v.
    """
    backend = backend_for_connection(conn)
    spec = URBAN_FORM_BLOCK_COMPACTNESS
    catalog_repo = backend.indicator_catalog_repo()
    catalog = catalog_repo.get_indicator(spec.slug)
    if catalog is None:
        raise RuntimeError(
            f"Indicator {spec.slug!r} not found in indicator_catalog. "
            "Run `indicators seed-catalog` first."
        )
    block_type_id = get_type_id(conn, "MEX", "block")

    job_run_repo = backend.job_run_repo()
    job_run_id = job_run_repo.start_job(
        model_family=spec.model_family or "urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
    )
    try:
        rows = backend.feature_repo().geodesic_metrics_by_type(
            type_id=block_type_id,
            aoi_id=aoi_id,
        )
        numeric_rows = [
            (feature_id, polsby_popper(area_m2=area, perimeter_m=perimeter))
            for feature_id, area, perimeter in rows
        ]
        result_repo = backend.indicator_result_repo()
        rows_written = result_repo.bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )
        job_run_repo.complete_job(job_run_id, status="succeeded")
    except Exception as exc:
        job_run_repo.complete_job(
            job_run_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(rows),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )


