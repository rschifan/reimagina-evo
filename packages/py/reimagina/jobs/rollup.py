"""Rollup worker: aggregate indicator_result from fine to coarse grain.

Reads current `indicator_result` rows at a source feature_type, joins them
to `feature_overlap` rows where the target feature has `type_slug` =
`target_feature_type`, applies the indicator's `aggregation_method`
(deferring to `reimagina.jobs.aggregation` for the actual math), and writes
one parent-grain `indicator_result` row per target feature with
`quality_flag='estimated'` — matching the contract in JOB_LAYER.md.

The worker is synchronous for v1: callers invoke it directly from a CLI or
an integration test. Once the queue arrives this module is the natural seam
to enqueue rollup messages from.
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
class RollupResult:
    """Outcome of a single rollup run."""

    job_run_id: str
    indicator_slug: str
    source_feature_type: str
    target_feature_type: str
    aggregation_method: str
    targets_considered: int
    rows_written: int


def run_rollup(
    conn,
    *,
    indicator_slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None = None,
    code_version: str = "dev",
    missing_threshold: float = 0.5,
) -> RollupResult:
    """Roll `indicator_slug` from `source_feature_type` up to `target_feature_type`.

    `missing_threshold` is the fraction of child weight that can be missing
    before the parent is written as quality_flag='missing'. Defaults to 0.5
    (>50% missing → parent missing, else prorate), matching JOB_LAYER.md.

    The rollup is idempotent: it writes through write_result, which stales any
    previous 'current' parent row for the same (feature, scenario, indicator).
    """
    backend = backend_for_connection(conn)
    catalog_repo = backend.indicator_catalog_repo()
    catalog = catalog_repo.get_indicator(indicator_slug)
    if catalog is None:
        raise ValueError(f"Unknown indicator slug: {indicator_slug!r}")
    aggregation_method = catalog["aggregation_method"]
    if aggregation_method == "none":
        raise ValueError(
            f"Indicator {indicator_slug!r} has aggregation_method='none' — not rollable."
        )

    applies_to = list(catalog["applies_to"] or [])
    if target_feature_type not in applies_to:
        raise ValueError(
            f"target_feature_type {target_feature_type!r} is not in the indicator's "
            f"applies_to set {applies_to!r}. Add it to the catalog before rolling up."
        )

    job_run_repo = backend.job_run_repo()
    job_run_id = job_run_repo.start_job(
        model_family="rollup",
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
    )
    rows_written = 0
    try:
        groups = _group_children_by_parent(
            backend=backend,
            indicator_slug=indicator_slug,
            source_feature_type=source_feature_type,
            target_feature_type=target_feature_type,
            scenario_id=scenario_id,
        )
        result_repo = backend.indicator_result_repo()
        for target_feature_id, children in groups.items():
            rows_written += _write_rollup_row(
                result_repo=result_repo,
                indicator_slug=indicator_slug,
                target_feature_id=target_feature_id,
                children=children,
                scenario_id=scenario_id,
                job_run_id=job_run_id,
                aggregation_method=aggregation_method,
                missing_threshold=missing_threshold,
            )
        job_run_repo.complete_job(job_run_id, status="succeeded")
    except Exception as exc:
        job_run_repo.complete_job(
            job_run_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    return RollupResult(
        job_run_id=job_run_id,
        indicator_slug=indicator_slug,
        source_feature_type=source_feature_type,
        target_feature_type=target_feature_type,
        aggregation_method=aggregation_method,
        targets_considered=len(groups),
        rows_written=rows_written,
    )


# ─── Fetch helpers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ChildRow:
    value: float | None
    weight: float
    quality_flag: str


def _group_children_by_parent(
    *,
    backend: Any,
    indicator_slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None,
) -> dict[str, list[_ChildRow]]:
    """Return {target_feature_id: [child rows]} joining overlap × indicator_result.

    Pulls only current base- or scenario-scoped rows as requested. NULL-safe
    comparison on scenario_id is handled by a per-backend branch so both
    Postgres and DuckDB treat "base state" the same way.
    """
    conn = backend.connection
    if backend.name == "postgres":
        return _group_children_postgres(
            conn,
            indicator_slug=indicator_slug,
            source_feature_type=source_feature_type,
            target_feature_type=target_feature_type,
            scenario_id=scenario_id,
        )
    return _group_children_duckdb(
        conn,
        indicator_slug=indicator_slug,
        source_feature_type=source_feature_type,
        target_feature_type=target_feature_type,
        scenario_id=scenario_id,
    )


def _group_children_postgres(
    conn,
    *,
    indicator_slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None,
) -> dict[str, list[_ChildRow]]:
    scenario_predicate = (
        "ir.scenario_id IS NULL" if scenario_id is None else "ir.scenario_id = %s"
    )
    # SQL placeholder order: src.type_slug, tgt.type_slug, ir.indicator_slug,
    # [optional ir.scenario_id]. Keeps the parameter list lined up with the
    # join-order we read in the query.
    params: list = [source_feature_type, target_feature_type, indicator_slug]
    if scenario_id is not None:
        params.append(scenario_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT fo.target_feature_id::text,
                   ir.value_numeric,
                   fo.overlap_ratio,
                   ir.quality_flag
            FROM curated_geo.feature_overlap fo
            JOIN curated_geo.feature src ON src.id = fo.source_feature_id
                 AND src.type_slug = %s
            JOIN curated_geo.feature tgt ON tgt.id = fo.target_feature_id
                 AND tgt.type_slug = %s
            LEFT JOIN curated_geo.indicator_result ir
                 ON ir.feature_id = fo.source_feature_id
                AND ir.indicator_slug = %s
                AND ir.status = 'current'
                AND {scenario_predicate}
            """,
            params,
        )
        groups: dict[str, list[_ChildRow]] = {}
        for target_id, value, weight, quality in cur.fetchall():
            groups.setdefault(str(target_id), []).append(
                _ChildRow(
                    value=float(value) if value is not None else None,
                    weight=float(weight),
                    quality_flag=quality or "missing",
                )
            )
    return groups


def _group_children_duckdb(
    conn,
    *,
    indicator_slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None,
) -> dict[str, list[_ChildRow]]:
    scenario_predicate = (
        "ir.scenario_id IS NULL"
        if scenario_id is None
        else "CAST(ir.scenario_id AS VARCHAR) = ?"
    )
    query = f"""
        SELECT CAST(fo.target_feature_id AS VARCHAR),
               ir.value_numeric,
               fo.overlap_ratio,
               ir.quality_flag
        FROM curated_geo.feature_overlap fo
        JOIN curated_geo.feature src
          ON src.id = fo.source_feature_id AND src.type_slug = ?
        JOIN curated_geo.feature tgt
          ON tgt.id = fo.target_feature_id AND tgt.type_slug = ?
        LEFT JOIN curated_geo.indicator_result ir
          ON ir.feature_id = fo.source_feature_id
         AND ir.indicator_slug = ?
         AND ir.status = 'current'
         AND {scenario_predicate}
    """
    params = [source_feature_type, target_feature_type, indicator_slug]
    if scenario_id is not None:
        params.append(scenario_id)

    groups: dict[str, list[_ChildRow]] = {}
    for target_id, value, weight, quality in conn.execute(query, params).fetchall():
        groups.setdefault(str(target_id), []).append(
            _ChildRow(
                value=float(value) if value is not None else None,
                weight=float(weight),
                quality_flag=quality or "missing",
            )
        )
    return groups


# ─── Aggregation + write ───────────────────────────────────────────────────


def _write_rollup_row(
    *,
    result_repo: Any,
    indicator_slug: str,
    target_feature_id: str,
    children: list[_ChildRow],
    scenario_id: str | None,
    job_run_id: str,
    aggregation_method: str,
    missing_threshold: float,
) -> int:
    total_weight = sum(child.weight for child in children)
    missing_weight = sum(
        child.weight
        for child in children
        if child.value is None or child.quality_flag in ("missing", "not_applicable", "failed")
    )
    share_state = classify_missing_share(
        total_weight=total_weight,
        missing_weight=missing_weight,
        threshold=missing_threshold,
    )
    if share_state == "missing":
        result_repo.write_result(
            feature_id=target_feature_id,
            indicator_slug=indicator_slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
            quality_flag="missing",
            missing_reason=(
                f"rollup: missing fraction {missing_weight / total_weight if total_weight > 0 else 1.0:.2f} "
                f"exceeds threshold {missing_threshold}"
            ),
        )
        return 1

    usable = [
        (child.value, child.weight)
        for child in children
        if child.value is not None
        and child.quality_flag not in ("missing", "not_applicable", "failed")
    ]
    try:
        aggregated = aggregate(aggregation_method, usable)
    except (MissingAggregationInputs, UnsupportedAggregationMethod) as exc:
        # No usable inputs even though total_weight > 0 — write missing so
        # the parent surfaces the gap rather than silently skipping.
        result_repo.write_result(
            feature_id=target_feature_id,
            indicator_slug=indicator_slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
            quality_flag="missing",
            missing_reason=f"rollup: {type(exc).__name__}: {exc}",
        )
        return 1

    result_repo.write_result(
        feature_id=target_feature_id,
        indicator_slug=indicator_slug,
        job_run_id=job_run_id,
        scenario_id=scenario_id,
        quality_flag="estimated",
        value_numeric=aggregated,
    )
    return 1
