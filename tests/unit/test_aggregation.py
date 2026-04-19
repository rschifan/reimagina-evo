"""Unit tests for the pure aggregation helpers used by the rollup worker."""
from __future__ import annotations

import pytest

from reimagina.jobs.aggregation import (
    MissingAggregationInputs,
    UnsupportedAggregationMethod,
    aggregate,
    classify_missing_share,
)


def test_mean_ignores_weights():
    assert aggregate("mean", [(2.0, 5.0), (4.0, 99.0), (6.0, 0.1)]) == 4.0


def test_weighted_mean_uses_weights():
    # Two children with equal value but wildly different weights → mean value.
    assert aggregate("weighted_mean", [(10.0, 0.1), (10.0, 0.9)]) == 10.0
    # Two children, one at 0 with tiny weight and one at 100 with dominant weight.
    assert aggregate("weighted_mean", [(0.0, 0.1), (100.0, 0.9)]) == 90.0


def test_sum_is_numeric_sum():
    assert aggregate("sum", [(1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]) == 6.0


def test_min_and_max():
    rows = [(2.0, 1.0), (5.0, 1.0), (3.0, 1.0)]
    assert aggregate("min", rows) == 2.0
    assert aggregate("max", rows) == 5.0


def test_empty_input_raises_missing_signal():
    with pytest.raises(MissingAggregationInputs):
        aggregate("mean", [])


def test_weighted_mean_with_zero_total_weight_raises_missing_signal():
    with pytest.raises(MissingAggregationInputs):
        aggregate("weighted_mean", [(1.0, 0.0), (2.0, 0.0)])


def test_unknown_method_raises():
    with pytest.raises(UnsupportedAggregationMethod):
        aggregate("median", [(1.0, 1.0)])


def test_classify_missing_share_complete_prorated_missing():
    assert classify_missing_share(total_weight=10.0, missing_weight=0.0) == "complete"
    assert classify_missing_share(total_weight=10.0, missing_weight=3.0) == "prorated"
    assert classify_missing_share(total_weight=10.0, missing_weight=6.0) == "missing"
    # Zero total weight short-circuits to missing regardless.
    assert classify_missing_share(total_weight=0.0, missing_weight=0.0) == "missing"
    # Custom threshold.
    assert (
        classify_missing_share(total_weight=10.0, missing_weight=3.0, threshold=0.25)
        == "missing"
    )
