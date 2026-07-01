"""Unit tests for the single-source indicator_result validator — pure, no DB."""

from __future__ import annotations

import pytest

from reimagina.indicators._hamilton_driver import build_indicator_driver
from reimagina.indicators.validation import validate_result_rows

_KW = {
    "applies_to": ["block"],
    "valid_min": 0.0,
    "valid_max": 1.0,
    "run_scenario": None,
}


def _row(**overrides):
    base = {
        "value_numeric": 0.5,
        "value_text": None,
        "value_json": None,
        "quality_flag": "measured",
        "status": "current",
        "missing_reason": None,
        "feature_type": "block",
        "scenario_id": None,
    }
    base.update(overrides)
    return base


def test_valid_row_passes():
    validate_result_rows([_row()], **_KW)


def test_above_valid_max_rejected():
    with pytest.raises(ValueError, match="above valid_max"):
        validate_result_rows([_row(value_numeric=2.0)], **_KW)


def test_below_valid_min_rejected():
    with pytest.raises(ValueError, match="below valid_min"):
        validate_result_rows([_row(value_numeric=-1.0)], **_KW)


def test_feature_type_not_in_applies_to_rejected():
    with pytest.raises(ValueError, match="applies_to"):
        validate_result_rows([_row(feature_type="lot")], **_KW)


def test_two_value_columns_rejected():
    with pytest.raises(ValueError, match="exactly one value column"):
        validate_result_rows([_row(value_text="x")], **_KW)


def test_missing_reason_required_for_missing_quality():
    with pytest.raises(ValueError, match="missing_reason"):
        validate_result_rows([_row(quality_flag="missing", value_numeric=None)], **_KW)


def test_scenario_mismatch_rejected():
    with pytest.raises(ValueError, match="must match"):
        validate_result_rows([_row(scenario_id="s1")], **_KW)


def test_check_output_rejects_out_of_bounds_in_graph():
    # density kernel is unbounded (cap 500/ha); a huge count trips @check_output.
    dr = build_indicator_driver()
    with pytest.raises(Exception, match="above valid_max"):
        dr.execute(
            ["density_result_frame"],
            inputs={"centroid_counts": [("b1", 1000, 1.0)]},
        )
