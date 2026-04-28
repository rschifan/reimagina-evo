"""Pure aggregation helpers for the rollup worker.

This module is intentionally side-effect free: it does not touch the
database or any worker state. Workers (once the indicator layer lands)
will fetch fine-grain values and weights, then pass them here.

The five supported aggregation methods mirror `indicator_catalog.aggregation_method`.
"""

from __future__ import annotations

import statistics
from typing import Sequence

AggregationMethod = str  # 'mean' | 'weighted_mean' | 'sum' | 'min' | 'max'


class MissingAggregationInputs(ValueError):
    """Raised when an aggregation is invoked with no inputs at all."""


class UnsupportedAggregationMethod(ValueError):
    """Raised for aggregation_method values outside the supported set."""


def aggregate(
    method: AggregationMethod,
    values_and_weights: Sequence[tuple[float, float]],
) -> float:
    """Aggregate `(value, weight)` pairs with the requested method.

    Weights are ignored for methods other than `weighted_mean`, but callers
    should still pass them so that the upstream missing-input policy can be
    computed uniformly. An empty input raises MissingAggregationInputs so
    the caller can classify the parent result as `missing`.
    """
    if not values_and_weights:
        raise MissingAggregationInputs("no inputs provided to aggregate()")

    if method == "mean":
        return statistics.fmean(v for v, _ in values_and_weights)

    if method == "weighted_mean":
        total_weight = sum(w for _, w in values_and_weights)
        if total_weight <= 0:
            raise MissingAggregationInputs("total weight is zero for weighted_mean")
        return sum(v * w for v, w in values_and_weights) / total_weight

    if method == "sum":
        return float(sum(v for v, _ in values_and_weights))

    if method == "min":
        return float(min(v for v, _ in values_and_weights))

    if method == "max":
        return float(max(v for v, _ in values_and_weights))

    raise UnsupportedAggregationMethod(
        f"unknown aggregation_method: {method!r}; expected one of "
        "'mean', 'weighted_mean', 'sum', 'min', 'max'"
    )


def classify_missing_share(
    *,
    total_weight: float,
    missing_weight: float,
    threshold: float = 0.5,
) -> str:
    """Return 'missing', 'prorated', or 'complete' based on missing-input share.

    threshold is the fraction above which the parent should be emitted as
    `missing`. The default (> 50%) matches the prototype policy documented
    in JOB_LAYER.md; the indicator catalog may eventually override it
    per-indicator.
    """
    if total_weight <= 0:
        return "missing"
    share = missing_weight / total_weight
    if share > threshold:
        return "missing"
    if share > 0:
        return "prorated"
    return "complete"
