"""CLI helpers for Postgres/PostGIS performance monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reimagina.db import managed_connection
from reimagina.perf import (
    build_observability_report,
    compare_reports,
    explain_benchmark,
    prepare_benchmark_dataset,
    run_benchmarks,
)


def seed_observations(
    *, dsn: str, dataset_tier: str, feature_limit: int | None = None
) -> dict[str, Any]:
    with managed_connection(dsn) as conn:
        result = prepare_benchmark_dataset(
            conn,
            dataset_tier=dataset_tier,
            feature_limit=feature_limit,
        )
        return {"dsn": dsn, **result}


def run_perf_benchmarks(
    *,
    dsn: str,
    dataset_tier: str,
    benchmark_names: list[str] | None = None,
    iterations: int | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    with managed_connection(dsn) as conn:
        result = run_benchmarks(
            conn,
            dataset_tier=dataset_tier,
            benchmark_names=benchmark_names,
            iterations=iterations,
        )
    payload = {"dsn": dsn, **result}
    if output_path:
        Path(output_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
        payload["output_path"] = output_path
    return payload


def explain_query(
    *, dsn: str, benchmark_name: str, dataset_tier: str
) -> dict[str, Any]:
    with managed_connection(dsn) as conn:
        return {
            "dsn": dsn,
            **explain_benchmark(
                conn,
                benchmark_name=benchmark_name,
                dataset_tier=dataset_tier,
            ),
        }


def performance_report(*, dsn: str, limit: int = 10) -> dict[str, Any]:
    with managed_connection(dsn) as conn:
        return {"dsn": dsn, "report": build_observability_report(conn, limit=limit)}


def compare_perf_reports(*, baseline_path: str, candidate_path: str) -> dict[str, Any]:
    return compare_reports(baseline_path=baseline_path, candidate_path=candidate_path)
