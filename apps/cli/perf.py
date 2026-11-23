"""CLI helpers for Postgres/PostGIS performance monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apps.cli import UserError
from reimagina.db import connect
from reimagina.storage import mask_dsn
from reimagina.perf import (
    BENCHMARKS,
    build_observability_report,
    cleanup_perf_data,
    compare_reports,
    explain_benchmark,
    prepare_benchmark_dataset,
    run_benchmarks,
)
from reimagina.perf.runner import NO_PARAMETER_BUILDER


def _require_runnable(names: list[str]) -> None:
    """Refuse a name the runner cannot run, before connecting.

    Left to the runner it raised a bare ValueError, which `main` keeps as a
    traceback because a ValueError may be a defect; a typo is not.
    """
    for name in names:
        if name not in BENCHMARKS:
            raise UserError(f"unknown benchmark {name!r}; see perf.manifest")
        if name in NO_PARAMETER_BUILDER:
            raise UserError(
                f"benchmark {name!r} is declared but has no parameter builder yet"
            )


def seed_observations(
    *,
    dsn: str,
    dataset_tier: str,
    feature_limit: int | None = None,
    aoi_id: str | None = None,
) -> dict[str, Any]:
    with connect(dsn) as conn:
        result = prepare_benchmark_dataset(
            conn,
            dataset_tier=dataset_tier,
            feature_limit=feature_limit,
            aoi_id=aoi_id,
        )
        return {"dsn": dsn, **result}


def cleanup_perf(*, dsn: str, dry_run: bool = False) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, **cleanup_perf_data(conn, dry_run=dry_run)}


def run_perf_benchmarks(
    *,
    dsn: str,
    dataset_tier: str,
    benchmark_names: list[str] | None = None,
    iterations: int | None = None,
    output_path: str | None = None,
    aoi_id: str | None = None,
) -> dict[str, Any]:
    _require_runnable(benchmark_names or [])
    with connect(dsn) as conn:
        result = run_benchmarks(
            conn,
            dataset_tier=dataset_tier,
            benchmark_names=benchmark_names,
            iterations=iterations,
            aoi_id=aoi_id,
        )
    payload = {"dsn": dsn, **result}
    if output_path:
        # The file is kept and passed around; only stdout goes through `_print`'s mask.
        Path(output_path).write_text(
            json.dumps(
                {**payload, "dsn": mask_dsn(dsn)}, indent=2, sort_keys=True, default=str
            )
        )
        payload["output_path"] = output_path
    return payload


def explain_query(
    *, dsn: str, benchmark_name: str, dataset_tier: str, aoi_id: str | None = None
) -> dict[str, Any]:
    _require_runnable([benchmark_name])
    with connect(dsn) as conn:
        return {
            "dsn": dsn,
            **explain_benchmark(
                conn,
                benchmark_name=benchmark_name,
                dataset_tier=dataset_tier,
                aoi_id=aoi_id,
            ),
        }


def performance_report(*, dsn: str, limit: int = 10) -> dict[str, Any]:
    with connect(dsn) as conn:
        return {"dsn": dsn, "report": build_observability_report(conn, limit=limit)}


def compare_perf_reports(*, baseline_path: str, candidate_path: str) -> dict[str, Any]:
    return compare_reports(baseline_path=baseline_path, candidate_path=candidate_path)
