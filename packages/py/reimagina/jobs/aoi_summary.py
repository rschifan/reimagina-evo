"""AOI summary worker: aggregate current indicator_result across an AOI.

Reads the current per-feature indicator values for a given AOI (joining
`aoi_feature` for per-feature weights + `indicator_result` for values),
aggregates using the indicator's catalog `aggregation_method`, and upserts
one row per (aoi_id, scenario_id, indicator_id) into `aoi_indicator_summary`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reimagina.jobs.aggregation import (
    MissingAggregationInputs,
    UnsupportedAggregationMethod,
    aggregate,
    classify_missing_share,
)
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

    children = _fetch_aoi_children(
        backend=backend,
        aoi_id=aoi_id,
        indicator_slug=indicator_slug,
        scenario_id=scenario_id,
    )
    total_weight = sum(child[1] for child in children)
    missing_weight = sum(
        weight
        for value, weight, quality in children
        if value is None or quality in ("missing", "not_applicable", "failed")
    )
    share_state = classify_missing_share(
        total_weight=total_weight,
        missing_weight=missing_weight,
        threshold=missing_threshold,
    )

    summary_repo = backend.aoi_summary_repo()
    if share_state == "missing":
        note = (
            f"missing fraction {missing_weight / total_weight if total_weight > 0 else 1.0:.2f} "
            f"exceeds threshold {missing_threshold}"
        )
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

    usable = [
        (value, weight)
        for value, weight, quality in children
        if value is not None
        and quality not in ("missing", "not_applicable", "failed")
    ]
    try:
        aggregated = aggregate(aggregation_method, usable)
    except (MissingAggregationInputs, UnsupportedAggregationMethod) as exc:
        note = f"{type(exc).__name__}: {exc}"
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
        f"{aggregation_method} over {len(usable)} of {len(children)} children"
        + ("; prorated" if share_state == "prorated" else "")
    )
    summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=aggregated,
        scenario_id=scenario_id,
        aggregation_note=note,
        status="current",
    )
    return AOISummaryResult(
        aoi_id=aoi_id,
        indicator_slug=indicator_slug,
        scenario_id=scenario_id,
        value=aggregated,
        status="current",
        aggregation_note=note,
    )


def _fetch_aoi_children(
    *,
    backend: Any,
    aoi_id: str,
    indicator_slug: str,
    scenario_id: str | None,
) -> list[tuple[float | None, float, str]]:
    """Return (value, weight, quality_flag) for each feature in the AOI.

    Uses curated_geo.aoi_feature for per-feature weights so the summary
    inherits whatever overlap basis the AOI compute used (area, length,
    presence). Features without a current indicator_result show up with
    value=None so the missing-weight policy can account for them.
    """
    conn = backend.connection
    if backend.name == "postgres":
        return _fetch_aoi_children_postgres(
            conn,
            aoi_id=aoi_id,
            indicator_slug=indicator_slug,
            scenario_id=scenario_id,
        )
    return _fetch_aoi_children_duckdb(
        conn,
        aoi_id=aoi_id,
        indicator_slug=indicator_slug,
        scenario_id=scenario_id,
    )


def _fetch_aoi_children_postgres(
    conn,
    *,
    aoi_id: str,
    indicator_slug: str,
    scenario_id: str | None,
) -> list[tuple[float | None, float, str]]:
    # SQL placeholder order: indicator_slug, [scenario_id], aoi_id — matching
    # the LEFT JOIN clause order and the final WHERE.
    scenario_predicate = (
        "ir.scenario_id IS NULL"
        if scenario_id is None
        else "ir.scenario_id = %s"
    )
    params: list = [indicator_slug]
    if scenario_id is not None:
        params.append(scenario_id)
    params.append(aoi_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ir.value_numeric,
                   af.overlap_ratio,
                   ir.quality_flag
            FROM curated_geo.aoi_feature af
            LEFT JOIN curated_geo.indicator_result ir
                 ON ir.feature_id = af.feature_id
                AND ir.indicator_slug = %s
                AND ir.status = 'current'
                AND {scenario_predicate}
            WHERE af.aoi_id = %s
            """,
            params,
        )
        return [
            (float(value) if value is not None else None, float(weight), quality or "missing")
            for value, weight, quality in cur.fetchall()
        ]


def _fetch_aoi_children_duckdb(
    conn,
    *,
    aoi_id: str,
    indicator_slug: str,
    scenario_id: str | None,
) -> list[tuple[float | None, float, str]]:
    scenario_predicate = (
        "ir.scenario_id IS NULL"
        if scenario_id is None
        else "CAST(ir.scenario_id AS VARCHAR) = ?"
    )
    query = f"""
        SELECT ir.value_numeric,
               af.overlap_ratio,
               ir.quality_flag
        FROM curated_geo.aoi_feature af
        LEFT JOIN curated_geo.indicator_result ir
          ON ir.feature_id = af.feature_id
         AND ir.indicator_slug = ?
         AND ir.status = 'current'
         AND {scenario_predicate}
        WHERE CAST(af.aoi_id AS VARCHAR) = ?
    """
    params: list = [indicator_slug]
    if scenario_id is not None:
        params.append(scenario_id)
    params.append(aoi_id)
    return [
        (float(value) if value is not None else None, float(weight), quality or "missing")
        for value, weight, quality in conn.execute(query, params).fetchall()
    ]
