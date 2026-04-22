"""Unit tests for the stat-name mapping + default stat sets.

The worker uses short names (``p05``, ``p95``) in the observation
``attribute_key`` and translates them to rasterstats' own names
(``percentile_5``, ``percentile_95``) only when invoking rasterstats.
The translation table must round-trip correctly — otherwise observations
would land under the wrong key or rasterstats would raise on unknown
stat names.
"""
from __future__ import annotations

import pytest

from reimagina.jobs.raster_zonal_stats import (
    _DEFAULT_STATS_BY_KIND,
    _STAT_ALIAS,
    _rasterstats_name,
)


def test_percentile_aliases_match_rasterstats_spelling():
    assert _rasterstats_name("p05") == "percentile_5"
    assert _rasterstats_name("p95") == "percentile_95"


def test_non_percentile_stats_pass_through_unchanged():
    for stat in ("mean", "median", "std", "sum", "count"):
        assert _rasterstats_name(stat) == stat


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


def test_stat_alias_is_closed_under_the_default_set():
    # Every percentile appearing in the default intensive set must have
    # a translation to rasterstats — otherwise the worker would pass
    # an unrecognised name to rasterstats and raise.
    intensive = _DEFAULT_STATS_BY_KIND["intensive"]
    for stat in intensive:
        if stat.startswith("p"):
            assert stat in _STAT_ALIAS, f"missing rasterstats alias for {stat!r}"
