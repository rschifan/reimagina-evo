"""Unit coverage for the shared storage helpers extracted from the repositories.

`route_value` and `value_stats_dict` were lifted verbatim from identical blocks
in the Postgres and DuckDB repos; these tests pin their behavior directly (the
`value_stats` path has no integration coverage).
"""

from __future__ import annotations

from reimagina.storage._utils import route_value, value_stats_dict


def test_route_value_numeric() -> None:
    assert route_value(3.5, "numeric") == (3.5, None, None)


def test_route_value_text() -> None:
    assert route_value("hi", "text") == (None, "hi", None)


def test_route_value_json_serialises() -> None:
    assert route_value({"a": 1}, "json") == (None, None, '{"a": 1}')


def test_route_value_unknown_type_is_all_none() -> None:
    assert route_value(7, "weird") == (None, None, None)


def test_value_stats_dict_coerces_types() -> None:
    out = value_stats_dict((5, 0, 1, 2, 3, 4))
    assert out == {
        "count": 5,
        "min": 0.0,
        "p05": 1.0,
        "p50": 2.0,
        "p95": 3.0,
        "max": 4.0,
    }
    assert isinstance(out["count"], int)
    assert isinstance(out["min"], float)


def test_value_stats_dict_preserves_nulls() -> None:
    out = value_stats_dict((0, None, None, None, None, None))
    assert out == {
        "count": 0,
        "min": None,
        "p05": None,
        "p50": None,
        "p95": None,
        "max": None,
    }
