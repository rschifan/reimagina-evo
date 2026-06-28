"""AOI summary worker: aggregate current indicator_result across an AOI.

Reads the current per-feature indicator values for a given AOI (joining
`aoi_feature` for per-feature weights + `indicator_result` for values),
aggregates using the indicator's catalog `aggregation_method`, and upserts
one row per (aoi_id, scenario_id, indicator_id) into `aoi_indicator_summary`.
"""

from __future__ import annotations

from dataclasses import dataclass

from reimagina.jobs.aggregation import aggregate_children
from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class AOISummaryResult:
    """Outcome of a single AOI summary run."""

    aoi_id: str
    indicator_slug: str
    scenario_id: str | None
    value: float | None
    status: str  # 'current' | 'missing'
    aggregation_note: str | None


def summarise_aoi(
    conn,
    *,
    aoi_id: str,
    indicator_slug: str,
    scenario_id: str | None = None,
    missing_threshold: float = 0.5,
) -> AOISummaryResult:
    """Compute and upsert one aoi_indicator_summary row.

    The aggregation follows the indicator's catalog `aggregation_method`,
    with the same missing-weight policy the rollup worker uses. A summary
    with all inputs missing is stored explicitly as status='missing' so
    consumers can distinguish 'no data' from 'not yet computed'.
    """
    backend = backend_for_connection(conn)
    catalog = backend.indicator_catalog_repo().get_indicator(indicator_slug)
    if catalog is None:
        raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
    aggregation_method = catalog["aggregation_method"]
    if aggregation_method == "none":
        raise ValueError(
            f"Indicator {indicator_slug!r} has aggregation_method='none' — not summarisable."
        )
    indicator_id = catalog["id"]

    children = backend.aoi_summary_repo().fetch_aoi_children(
        aoi_id=aoi_id,
        indicator_slug=indicator_slug,
        scenario_id=scenario_id,
    )
    outcome = aggregate_children(
        children,
        method=aggregation_method,
        threshold=missing_threshold,
    )
    summary_repo = backend.aoi_summary_repo()
    if outcome.status == "missing":
        note = outcome.reason
        summary_repo.upsert_summary(
            aoi_id=aoi_id,
            indicator_id=indicator_id,
            value=None,
            scenario_id=scenario_id,
            aggregation_note=note,
            status="missing",
        )
        return AOISummaryResult(
            aoi_id=aoi_id,
            indicator_slug=indicator_slug,
            scenario_id=scenario_id,
            value=None,
            status="missing",
            aggregation_note=note,
        )

    note = (
        f"{aggregation_method} over {outcome.usable_count} of "
        f"{outcome.total_count} children"
    ) + ("; prorated" if outcome.prorated else "")
    summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=outcome.value,
        scenario_id=scenario_id,
        aggregation_note=note,
        status="current",
    )
    return AOISummaryResult(
        aoi_id=aoi_id,
        indicator_slug=indicator_slug,
        scenario_id=scenario_id,
        value=outcome.value,
        status="current",
        aggregation_note=note,
    )
