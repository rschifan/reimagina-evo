"""Materialisation refuses only what the compute would actually write.

A block missing one of the three edificability inputs is skipped by
`edificability._edificability_rows`, so it gets no density row; refusing it on
density told the user to lower a number that could not breach anything.
"""

from __future__ import annotations

import pytest

from reimagina.scenarios import authoring


class _Cursor:
    """Answers the two reads: the baseline values, then the block areas."""

    def __init__(self, baseline: dict[str, float]) -> None:
        self._baseline = baseline
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, *_a):
        if "observation" in sql:
            self._rows = [("block-x", k, v) for k, v in self._baseline.items()]
        else:
            self._rows = [("block-x", 10_000.0)]

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, baseline: dict[str, float]) -> None:
        self._baseline = baseline

    def cursor(self):
        return _Cursor(self._baseline)


def _resolution(resolved: dict[tuple[str, str], float]) -> authoring._Resolution:
    return authoring._Resolution(
        matched={"rule-1": 1},
        values=resolved,
        owner=dict.fromkeys(resolved, "rule-1"),
    )


def test_a_block_the_compute_skips_is_not_refused_on_density():
    # 12 storeys at 50 m² is 2,400/ha > 2,000, but without people_per_household
    # the compute writes no density for this block.
    authoring._refuse_ceiling_breaches(
        _Conn({"unit_size": 50.0}),
        _resolution({("block-x", "num_levels"): 12.0, ("block-x", "floors"): 12.0}),
    )


def test_the_floors_bound_still_applies_to_such_a_block():
    with pytest.raises(authoring.CeilingExceeded) as caught:
        authoring._refuse_ceiling_breaches(
            _Conn({"unit_size": 50.0}),
            _resolution(
                {("block-x", "num_levels"): 500.0, ("block-x", "floors"): 500.0}
            ),
        )
    # The refusal names the rule and the storeys the block would allow.
    assert caught.value.rules == {"block-x": (("rule-1", 1),)}
    (breach,) = caught.value.breaches
    assert caught.value.allowed_levels[("block-x", breach.indicator_slug)] == int(
        breach.bound
    )
    # Plain words: the rule and the storeys that fit, no block id or slug.
    message = str(caught.value)
    assert "Rule 1" in message and f"at most {int(breach.bound)} storeys" in message
    assert "block-x" not in message and "valid_max" not in message


def _change_refusal(units: dict[str, str], levels: float) -> authoring.CeilingExceeded:
    # Baseline 2 storeys, 50 m² per home: the density bound (2,000/ha) allows
    # 10 storeys on this block.
    baseline = {
        "num_levels": 2.0,
        "floors": 2.0,
        "unit_size": 50.0,
        "people_per_household": 3.0,
        "lot_coverage": 1.0,
    }
    resolution = authoring._Resolution(
        matched={"rule-1": 1},
        values={("block-x", "num_levels"): levels, ("block-x", "floors"): levels},
        owner={("block-x", "num_levels"): "rule-1", ("block-x", "floors"): "rule-1"},
        modes={"rule-1": ("change", units)},
    )
    with pytest.raises(authoring.CeilingExceeded) as caught:
        authoring._refuse_ceiling_breaches(_Conn(baseline), resolution)
    return caught.value


def test_a_change_rule_in_percent_is_told_the_largest_percent_that_fits():
    # +1000 % = 22 storeys; 10 fit, which is +400 % of the baseline 2.
    message = str(_change_refusal({"levels": "pct"}, 22.0))
    assert "Rule 1" in message
    assert "+400 %" in message
    assert "block-x" not in message and "valid_max" not in message


def test_a_change_rule_in_storeys_is_told_the_largest_change_not_the_total():
    # +20 storeys = 22; 10 fit, which is +8 storeys over the baseline 2.
    message = str(_change_refusal({"levels": "abs"}, 22.0))
    assert "+8 storeys" in message
