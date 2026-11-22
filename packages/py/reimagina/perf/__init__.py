"""Performance benchmark and observability helpers for Postgres/PostGIS."""

from .manifest import BENCHMARKS, DATASET_TIERS, BenchmarkSpec, LatencyBudget
from .observability import build_observability_report
from .runner import compare_reports, explain_benchmark, run_benchmarks
from .seed import cleanup_perf_data, prepare_benchmark_dataset, seed_observations

__all__ = [
    "BENCHMARKS",
    "DATASET_TIERS",
    "BenchmarkSpec",
    "LatencyBudget",
    "build_observability_report",
    "cleanup_perf_data",
    "compare_reports",
    "explain_benchmark",
    "prepare_benchmark_dataset",
    "run_benchmarks",
    "seed_observations",
]
