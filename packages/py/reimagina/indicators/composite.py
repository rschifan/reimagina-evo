"""Generic composite-indicator computation (Phase 3, F3).

A composite indicator is a weighted combination of its ``depends_on`` inputs'
current results, per feature. ``compute_composite`` reads each input's current
``indicator_result``, combines them per feature (only where every input has a
value), and writes the composite result. It works for any composite
``IndicatorSpec`` — the platform computes a new composite by registering a
catalog entry with weighted ``depends_on``, no new code.
"""

from __future__ import annotations

from reimagina.indicators._compute import ComputeResult as ComputeResult
from reimagina.indicators.catalog import IndicatorSpec
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection


def compute_composite(
    conn,
    *,
    spec: IndicatorSpec,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
    aoi_id: str | None = None,
) -> ComputeResult:
    """Compute ``spec`` = Σ weightᵢ · inputᵢ over features common to all inputs."""
    if spec.computation_kind != "composite" or not spec.depends_on:
        raise ValueError(f"{spec.slug!r} is not a composite with dependencies")

    backend = backend_for_connection(conn)
    result_repo = backend.indicator_result_repo()

    weighted_inputs: list[tuple[float, dict[str, float]]] = []
    for dep_slug, weight in spec.depends_on:
        rows = result_repo.list_current_for_indicator(
            indicator_slug=dep_slug, scenario_id=scenario_id
        )
        values = {
            str(r["feature_id"]): r["value_numeric"]
            for r in rows
            if r.get("value_numeric") is not None
        }
        weighted_inputs.append((weight or 0.0, values))

    common = set.intersection(*(set(v) for _, v in weighted_inputs))
    if feature_ids is not None:
        common &= {str(f) for f in feature_ids}

    numeric_rows = [
        (fid, sum(weight * values[fid] for weight, values in weighted_inputs))
        for fid in common
    ]

    with job_run(
        backend,
        model_family=spec.model_family or "composite",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        rows_written = result_repo.bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(common),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )
