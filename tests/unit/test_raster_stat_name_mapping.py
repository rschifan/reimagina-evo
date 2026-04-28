"""Unit tests for the stat-name mapping + default stat sets.

The worker uses short names (``p05``, ``p95``, ``std``) in the
observation ``attribute_key`` and translates them to ``exactextract``'s
own op-strings only when invoking the engine. The translation table
must round-trip correctly — otherwise observations would land under
the wrong key or exactextract would raise on an unknown op.
"""

from __future__ import annotations

import pytest

from reimagina.jobs.raster_zonal_stats import (
    _DEFAULT_STATS_BY_KIND,
    _STAT_TO_OP,
    _exactextract_op,
    _exactextract_output_col,
)


def test_percentile_aliases_use_quantile_op_with_column_alias():
    """exactextract takes ``quantile(q=0.05)`` as the input op but
    emits a column named ``quantile_5``. Both sides of the mapping
    must round-trip correctly or the worker will read a non-existent
    column and produce all-NaN rows."""
    assert _exactextract_op("p05") == "quantile(q=0.05)"
    assert _exactextract_output_col("p05") == "quantile_5"
    assert _exactextract_op("p95") == "quantile(q=0.95)"
    assert _exactextract_output_col("p95") == "quantile_95"


def test_std_and_median_translate_to_exactextract_names():
    # exactextract names the standard deviation ``stdev`` and exposes
    # the median through the quantile op.
    assert _exactextract_op("std") == "stdev"
    assert _exactextract_output_col("std") == "stdev"
    assert _exactextract_op("median") == "quantile(q=0.5)"
    assert _exactextract_output_col("median") == "quantile_50"


def test_non_aliased_stats_pass_through_unchanged():
    # ``sum``, ``count``, ``mean``, ``min``, ``max`` are exactextract's
    # native op names and must map to themselves on both sides.
    for stat in ("mean", "sum", "count", "min", "max"):
        assert _exactextract_op(stat) == stat
        assert _exactextract_output_col(stat) == stat


def test_default_intensive_stats_are_central_tendency_and_dispersion():
    intensive = _DEFAULT_STATS_BY_KIND["intensive"]
    assert "mean" in intensive
    assert "median" in intensive
    assert "p05" in intensive
    assert "p95" in intensive
    assert "std" in intensive
    # Sum on an intensive raster (e.g. temperature) is nonsensical —
    # guard against anyone adding it to the default set without
    # considering the domain semantics.
    assert "sum" not in intensive


def test_default_extensive_stats_are_aggregative():
    extensive = _DEFAULT_STATS_BY_KIND["extensive"]
    assert "sum" in extensive
    assert "count" in extensive
    # Mean on an extensive raster (e.g. population count) would
    # average per-pixel counts, which is meaningless. Same guard.
    assert "mean" not in extensive


def test_unknown_aggregation_kind_raises_keyerror():
    with pytest.raises(KeyError):
        _DEFAULT_STATS_BY_KIND["categorical"]


def test_stat_to_op_is_closed_under_the_default_intensive_set():
    """Every aliased stat in the intensive default must have a
    translation — otherwise the worker would pass an unrecognised op
    to exactextract and the engine would raise on the first feature."""
    intensive = _DEFAULT_STATS_BY_KIND["intensive"]
    for stat in intensive:
        # ``mean`` is exactextract-native and intentionally not in the
        # alias table; only the renamed / parameterised stats need a
        # translation entry.
        if stat in {"p05", "p95", "std", "median"}:
            assert stat in _STAT_TO_OP, f"missing exactextract op for {stat!r}"
