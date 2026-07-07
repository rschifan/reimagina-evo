"""Branch coverage for the shared missing-data policy (aggregate_children).

This is the fast safety net the codebase lacked: the rollup and AOI-summary
workers previously hand-wrote this policy and were only exercised by the
integration tests.
"""

from __future__ import annotations

from reimagina.jobs.aggregation import aggregate_children


def test_ok_complete() -> None:
    out = aggregate_children(
        [(2.0, 1.0, "measured"), (4.0, 1.0, "measured")],
        method="mean",
        threshold=0.5,
    )
    assert out.status == "ok"
    assert out.value == 3.0
    assert (out.usable_count, out.total_count) == (2, 2)
    assert out.prorated is False
    assert out.reason is None


def test_weighted_mean() -> None:
    out = aggregate_children(
        [(0.0, 1.0, "measured"), (10.0, 3.0, "measured")],
        method="weighted_mean",
        threshold=0.5,
    )
    assert out.status == "ok"
    assert out.value == 7.5  # (0*1 + 10*3) / 4


def test_prorated_below_threshold() -> None:
    # one missing child of weight 1 out of total 4 → share 0.25 ≤ 0.5 → prorated
    out = aggregate_children(
        [(2.0, 3.0, "measured"), (None, 1.0, "missing")],
        method="mean",
        threshold=0.5,
    )
    assert out.status == "ok"
    assert out.prorated is True
    assert (out.usable_count, out.total_count) == (1, 2)
    assert out.value == 2.0


def test_missing_by_threshold() -> None:
    out = aggregate_children(
        [(2.0, 1.0, "measured"), (None, 3.0, "missing")],
        method="mean",
        threshold=0.5,
    )
    assert out.status == "missing"
    assert out.value is None
    assert "missing fraction 0.75" in out.reason
    assert "exceeds threshold 0.5" in out.reason


def test_missing_on_unsupported_method() -> None:
    out = aggregate_children(
        [(2.0, 1.0, "measured")],
        method="median",  # not in the supported set
        threshold=0.5,
    )
    assert out.status == "missing"
    assert out.reason.startswith("UnsupportedAggregationMethod")


def test_all_children_empty_is_missing() -> None:
    out = aggregate_children([], method="mean", threshold=0.5)
    assert out.status == "missing"
    assert out.total_count == 0
