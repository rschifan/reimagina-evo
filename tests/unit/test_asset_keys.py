"""Partition-key canonicalisation.

Cheap tests with an expensive failure mode: the key is hashed as an opaque
string by both ``serving_geo.asset_expected_version_v`` and
``reimagina.assets.version``, so a change in how a partition renders silently
changes every version derived from it.
"""

from __future__ import annotations

import pytest

from reimagina.assets.keys import BASELINE, partition_key


def test_no_dimensions_is_the_baseline():
    assert partition_key() == BASELINE == ""
    assert partition_key({}) == BASELINE


def test_none_valued_dimension_is_the_baseline_not_an_empty_partition():
    """`scenario=None` is the baseline, not a partition that looks like it.

    Rendering it as `scenario=` would create a partition distinct from the
    baseline that no producer ever writes, so every consumer of it would report
    a permanent 'absent'.
    """
    assert partition_key({"scenario": None}) == BASELINE


def test_dimensions_are_sorted_by_name():
    # Insertion order must not reach the key: two callers building the same
    # partition from different dicts have to agree.
    assert (
        partition_key({"target": "ageb", "source": "block"})
        == partition_key({"source": "block", "target": "ageb"})
        == "source=block/target=ageb"
    )


def test_single_dimension():
    assert partition_key({"scenario": "s1"}) == "scenario=s1"


@pytest.mark.parametrize(
    "dimensions",
    [
        {"scenario": "a/b"},
        {"scenario": "a=b"},
        {"a/b": "x"},
        {"a=b": "x"},
    ],
)
def test_separators_in_a_dimension_are_rejected(dimensions):
    """Otherwise two different partitions can render to one key and merge."""
    with pytest.raises(ValueError, match="may not contain"):
        partition_key(dimensions)
