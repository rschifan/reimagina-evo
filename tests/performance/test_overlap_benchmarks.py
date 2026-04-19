"""Real-data performance check for block→AGEB overlap inside Cuauhtémoc.

Skipped unless the CDMX realistic-scale bootstrap has been run against the
test Postgres DB. When the data is missing, the runner raises
OverlapBenchmarkDataMissing — the test converts that into a pytest skip with
a clear, actionable message instead of failing.
"""
from __future__ import annotations

import pytest

from reimagina.perf import run_benchmarks
from reimagina.perf.runner import OverlapBenchmarkDataMissing


def test_overlap_block_to_ageb_cuauhtemoc_hits_budget(conn):
    """Runs the overlap benchmark once and asserts budget + index usage.

    The SQL is a dry-run SELECT with the same spatial predicate + geography
    math the production OverlapRepository uses, so a regression here is a
    signal that the production overlap compute will degrade too.
    """
    try:
        report = run_benchmarks(
            conn,
            dataset_tier="representative",
            benchmark_names=["overlap_block_to_ageb_cuauhtemoc"],
        )
    except OverlapBenchmarkDataMissing as exc:
        pytest.skip(str(exc))

    assert report["passed"], report
    assert len(report["benchmarks"]) == 1
    result = report["benchmarks"][0]

    # Empirically ~2,853 rows in Cuauhtémoc; don't pin to the exact number
    # (INEGI releases change it slightly) — just assert order of magnitude.
    assert result["row_count"] >= 1000, result

    # The benchmark passes if every percentile fits its budget. `passed=True`
    # at the top implies this, but we also check that the planner actually
    # chose the GIST index; otherwise the "fast" timing would be an illusion
    # produced by very small data.
    index_names = result["plan_summary"]["index_names"]
    assert "feature_geometry_gist" in index_names, index_names
