"""A source that overlaps several targets must not be counted in full in each.

`sum` gave a straddling source's whole value to every target it touched, so the
rolled-up total exceeded the input total. `sum` now apportions by the share of
the source inside each target; `mean`/`min`/`max` cannot split a value and
refuse such sources; a point on a shared boundary goes to one target only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reimagina.jobs.observation_rollup import _compute_rollup_records

# (target_id, source_id, attribute_key, value, overlap_measure, overlap_ratio, basis)
# s1 lies 75/25 across t1 and t2; s2 lies wholly inside t2.
_STRADDLING = [
    ("t1", "s1", "pop_sum", 100.0, 750.0, 0.75, "area"),
    ("t2", "s1", "pop_sum", 100.0, 250.0, 0.25, "area"),
    ("t2", "s2", "pop_sum", 40.0, 500.0, 1.0, "area"),
]
_INSIDE = [
    ("t1", "s1", "pop_sum", 100.0, 750.0, 1.0, "area"),
    ("t2", "s2", "pop_sum", 40.0, 500.0, 1.0, "area"),
]
# p1 sits on the t1/t2 boundary, so it is covered by both.
_BOUNDARY_POINT = [
    ("t2", "p1", "pop_n", 1.0, 1.0, 1.0, "presence"),
    ("t1", "p1", "pop_n", 1.0, 1.0, 1.0, "presence"),
    ("t2", "p2", "pop_n", 1.0, 1.0, 1.0, "presence"),
]


def _by_target(rows, method, weight_basis=None, recipe_id="r-1"):
    backend = SimpleNamespace(
        overlap_repo=lambda: SimpleNamespace(
            rollup_observation_child_rows=lambda **_kw: rows
        )
    )
    records, _ = _compute_rollup_records(
        backend=backend,
        input_prefix="pop",
        input_feature_type="building",
        target_feature_type="block",
        output_prefix="pop_block",
        method=method,
        weight_basis=weight_basis,
        aoi_id=None,
        source_id="s",
        ingestion_run_id="r",
        recipe_id=recipe_id,
    )
    return {r["feature_id"]: r["value"] for r in records}


def test_sum_apportions_a_straddling_source_by_overlap_share():
    out = _by_target(_STRADDLING, "sum")
    assert out == pytest.approx({"t1": 75.0, "t2": 25.0 + 40.0})
    assert sum(out.values()) == pytest.approx(140.0)


def test_sum_of_sources_wholly_inside_one_target_is_unchanged():
    assert _by_target(_INSIDE, "sum") == {"t1": 100.0, "t2": 40.0}


@pytest.mark.parametrize("method", ["mean", "min", "max"])
def test_unsplittable_methods_refuse_straddling_sources(method):
    with pytest.raises(ValueError, match=r"r-1.*1 building source"):
        _by_target(_STRADDLING, method)


@pytest.mark.parametrize("method", ["mean", "min", "max"])
def test_unsplittable_methods_still_work_when_every_source_nests(method):
    assert _by_target(_INSIDE, method) == {"t1": 100.0, "t2": 40.0}


def test_a_boundary_point_counts_in_one_target_only():
    # The lowest target id wins, whatever order the rows come in.
    assert _by_target(_BOUNDARY_POINT, "sum") == {"t1": 1.0, "t2": 1.0}
    assert _by_target(_BOUNDARY_POINT, "max") == {"t1": 1.0, "t2": 1.0}


def test_weighted_mean_keeps_area_weights():
    out = _by_target(_STRADDLING, "weighted_mean", weight_basis="area")
    assert out["t1"] == pytest.approx(100.0)
    assert out["t2"] == pytest.approx((100 * 250 + 40 * 500) / 750)
