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

from reimagina.jobs.aggregation import aggregate_children
from reimagina.jobs._lifecycle import job_run
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

    with job_run(
        backend,
        model_family="rollup",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        groups = _group_children_by_parent(
            backend=backend,
            indicator_slug=indicator_slug,
            source_feature_type=source_feature_type,
            target_feature_type=target_feature_type,
            scenario_id=scenario_id,
        )
        result_repo = backend.indicator_result_repo()
        rows_written = 0
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
    """Return {target_feature_id: [child rows]} from overlap × indicator_result.

    The repo emits a flat list; grouping by target is a worker concern so
    the SQL contract stays thin.
    """
    flat = backend.overlap_repo().rollup_child_rows(
        indicator_slug=indicator_slug,
        source_feature_type=source_feature_type,
        target_feature_type=target_feature_type,
        scenario_id=scenario_id,
    )
    groups: dict[str, list[_ChildRow]] = {}
    for target_id, value, weight, quality in flat:
        groups.setdefault(target_id, []).append(
            _ChildRow(value=value, weight=weight, quality_flag=quality)
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
    outcome = aggregate_children(
        [(child.value, child.weight, child.quality_flag) for child in children],
        method=aggregation_method,
        threshold=missing_threshold,
    )
    if outcome.status == "missing":
        result_repo.write_result(
            feature_id=target_feature_id,
            indicator_slug=indicator_slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
            quality_flag="missing",
            missing_reason=f"rollup: {outcome.reason}",
        )
        return 1

    result_repo.write_result(
        feature_id=target_feature_id,
        indicator_slug=indicator_slug,
        job_run_id=job_run_id,
        scenario_id=scenario_id,
        quality_flag="estimated",
        value_numeric=outcome.value,
    )
    return 1
