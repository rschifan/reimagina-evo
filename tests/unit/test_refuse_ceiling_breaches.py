"""Materialisation refuses only what the compute would actually write.

A block missing one of the three edificability inputs is skipped by
`edificability._edificability_rows`, so it gets no density row; refusing it on
density told the user to lower a number that could not breach anything.
"""

from __future__ import annotations

import pytest

from reimagina.scenarios import authoring


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_a):
        pass

    def fetchall(self):
        return [("block-x", 10_000.0)]


class _Conn:
    def cursor(self):
        return _Cursor()


def _resolution(resolved: dict[tuple[str, str], float]) -> authoring._Resolution:
    return authoring._Resolution(
        matched={"rule-1": 1},
        values=resolved,
        owner=dict.fromkeys(resolved, "rule-1"),
        feature_type={"block-x": "block-type"},
        numbers={"rule-1": 1},
    )


def _baseline(monkeypatch, values: dict[str, float]):
    monkeypatch.setattr(
        authoring,
        "effective_numeric",
        lambda _c, *, attribute_key, **_kw: (
            {"block-x": values[attribute_key]} if attribute_key in values else {}
        ),
    )


def test_a_block_the_compute_skips_is_not_refused_on_density(monkeypatch):
    # 12 storeys at 50 m² is 2,400/ha > 2,000, but without people_per_household
    # the compute writes no density for this block.
    _baseline(monkeypatch, {"unit_size": 50.0})

    authoring._refuse_ceiling_breaches(
        _Conn(),
        _resolution({("block-x", "num_levels"): 12.0, ("block-x", "floors"): 12.0}),
    )


def test_the_floors_bound_still_applies_to_such_a_block(monkeypatch):
    _baseline(monkeypatch, {"unit_size": 50.0})

    with pytest.raises(authoring.CeilingExceeded) as caught:
        authoring._refuse_ceiling_breaches(
            _Conn(),
            _resolution(
                {("block-x", "num_levels"): 500.0, ("block-x", "floors"): 500.0}
            ),
        )
    # The refusal names the rule and the storeys the block would allow.
    assert caught.value.rules == {"block-x": ("rule-1",)}
    (breach,) = caught.value.breaches
    assert caught.value.allowed_levels[("block-x", breach.indicator_slug)] == int(
        breach.bound
    )
    assert "Rule 1 would make block block-x" in str(caught.value)
