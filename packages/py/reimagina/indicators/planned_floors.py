"""Planned-floors indicator — the effective ``floors`` attribute per block.

A direct indicator that reads the *effective* floors (base ⊕ scenario override,
via ``reimagina.scenarios.overrides``). Its point is to make the what-if real:
a scenario that overrides ``floors`` recomputes this to the new value, while the
baseline keeps the base data. The first indicator that reads an attribute
(the others are geometry-derived), so it's what a scenario edit actually moves.
"""

from __future__ import annotations

from dataclasses import dataclass

from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_PLANNED_FLOORS
from reimagina.jobs._lifecycle import job_run
from reimagina.scenarios.overrides import effective_numeric
from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class ComputeResult:
    job_run_id: str
    indicator_slug: str
    features_considered: int
    rows_written: int
    aoi_id: str | None


def compute_planned_floors(
    conn,
    *,
    aoi_id: str | None = None,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Write the effective ``floors`` value per block as planned_floors."""
    backend = backend_for_connection(conn)
    spec = URBAN_FORM_BLOCK_PLANNED_FLOORS
    block_type_id = get_type_id(conn, "MEX", "block")
    effective = effective_numeric(
        conn,
        type_id=block_type_id,
        attribute_key="floors",
        scenario_id=scenario_id,
        feature_ids=feature_ids,
    )
    numeric_rows = list(effective.items())
    with job_run(
        backend,
        model_family=spec.model_family or "urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        rows_written = backend.indicator_result_repo().bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )
    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(effective),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )
