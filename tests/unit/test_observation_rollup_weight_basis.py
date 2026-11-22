"""A rollup recipe's ``weight_basis`` must decide the weight, not be ignored.

It was validated (``weighted_mean`` requires one) and then dropped: the weight
was always the overlap measure, so a recipe declaring ``presence`` over polygon
sources silently got area weighting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reimagina.jobs.observation_rollup import _compute_rollup_records

# Two polygon sources in one target: a large one valued 10, a small one 0.
_ROWS = [
    ("t1", "s1", "pop_sum", 10.0, 900.0, 1.0, "area"),
    ("t1", "s2", "pop_sum", 0.0, 100.0, 1.0, "area"),
]


def _records(weight_basis):
    backend = SimpleNamespace(
        overlap_repo=lambda: SimpleNamespace(
            rollup_observation_child_rows=lambda **_kw: _ROWS
        )
    )
    records, _ = _compute_rollup_records(
        backend=backend,
        input_prefix="pop",
        input_feature_type="block",
        target_feature_type="ageb",
        output_prefix="pop_ageb",
        method="weighted_mean",
        weight_basis=weight_basis,
        aoi_id=None,
        source_id="s",
        ingestion_run_id="r",
    )
    return records


def test_area_weights_by_overlap_area():
    assert _records("area")[0]["value"] == pytest.approx(9.0)


def test_presence_counts_each_source_once():
    assert _records("presence")[0]["value"] == pytest.approx(5.0)


def test_a_basis_the_overlaps_were_not_measured_in_is_refused():
    with pytest.raises(ValueError, match="length"):
        _records("length")
