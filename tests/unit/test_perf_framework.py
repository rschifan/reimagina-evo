from __future__ import annotations

import json

from reimagina.perf import BENCHMARKS, DATASET_TIERS, compare_reports


def test_perf_manifest_contains_expected_hot_queries():
    assert "bbox_features_small" in BENCHMARKS
    assert "polygon_features_dense" in BENCHMARKS
    assert "selected_features_current_observations" in BENCHMARKS
    assert "smoke" in DATASET_TIERS
    assert "representative" in DATASET_TIERS
    assert "stress" in DATASET_TIERS


def test_compare_reports_computes_p95_delta(tmp_path):
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(
        json.dumps(
            {
                "passed": True,
                "benchmarks": [
                    {
                        "benchmark": "bbox_features_small",
                        "passed": True,
                        "timing_ms": {"p95": 100},
                    },
                ],
            }
        )
    )
    candidate_path.write_text(
        json.dumps(
            {
                "passed": True,
                "benchmarks": [
                    {
                        "benchmark": "bbox_features_small",
                        "passed": True,
                        "timing_ms": {"p95": 125},
                    },
                ],
            }
        )
    )

    comparison = compare_reports(
        baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert comparison["candidate_passed"] is True
    assert comparison["comparisons"][0]["delta_p95_ms"] == 25.0
    assert comparison["comparisons"][0]["regressed"] is True


def test_compare_reports_does_not_call_noise_a_regression(tmp_path):
    """Any positive p95 delta used to be `regressed`, so 0.4 ms of noise on a
    3-iteration run failed `perf compare`."""

    def report(p95):
        return json.dumps(
            {
                "passed": True,
                "benchmarks": [
                    {"benchmark": "b", "passed": True, "timing_ms": {"p95": p95}}
                ],
            }
        )

    (tmp_path / "a.json").write_text(report(10.0))
    (tmp_path / "b.json").write_text(report(10.4))

    comparison = compare_reports(
        baseline_path=tmp_path / "a.json", candidate_path=tmp_path / "b.json"
    )

    assert comparison["comparisons"][0]["regressed"] is False
