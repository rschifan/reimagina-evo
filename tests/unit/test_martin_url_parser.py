"""Unit tests for ``_martin_base_and_default_function`` URL parsing.

Codex review flagged that the original parser split *any* tail-like
URL segment into ``(base, function_name)``. That broke under reverse-
proxy deployments where the Martin URL is served at a path prefix
(e.g. ``https://host/martin``): the parser would emit base=
``https://host`` + function=``martin``, producing tile URLs that 404.

The fix whitelists known function names. These tests pin both shapes
so a future regression is caught at unit time.
"""
from __future__ import annotations

import pytest

from reimagina.tiles.service import _martin_base_and_default_function


@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        # Bare host — caller didn't include a function. Default.
        ("http://localhost:3000", ("http://localhost:3000", "get_tile")),
        ("http://localhost:3000/", ("http://localhost:3000", "get_tile")),
        # Legacy `{base}/{function}` shape — the parser extracts the function.
        ("http://localhost:3000/get_tile", ("http://localhost:3000", "get_tile")),
        (
            "http://localhost:3000/indicator_overlay_tile",
            ("http://localhost:3000", "indicator_overlay_tile"),
        ),
        (
            "http://localhost:3000/aggregation_overlay_tile",
            ("http://localhost:3000", "aggregation_overlay_tile"),
        ),
    ],
)
def test_parser_keeps_legacy_function_extraction(
    input_url: str, expected: tuple[str, str],
) -> None:
    assert _martin_base_and_default_function(input_url) == expected


@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        # Path-prefix deployments — the prefix is part of the base.
        # Without the whitelist, the old parser split off `martin` /
        # `tiles` / `v1` as a function name.
        ("https://example.com/martin", ("https://example.com/martin", "get_tile")),
        ("https://example.com/api/v1", ("https://example.com/api/v1", "get_tile")),
        ("https://example.com/tiles", ("https://example.com/tiles", "get_tile")),
        # Path prefix + legacy function suffix together.
        (
            "https://example.com/martin/get_tile",
            ("https://example.com/martin", "get_tile"),
        ),
        (
            "https://proxy.internal/api/v1/indicator_overlay_tile",
            ("https://proxy.internal/api/v1", "indicator_overlay_tile"),
        ),
    ],
)
def test_parser_preserves_path_prefix(
    input_url: str, expected: tuple[str, str],
) -> None:
    assert _martin_base_and_default_function(input_url) == expected


def test_parser_handles_string_without_scheme() -> None:
    """Older test fixtures pass a host:port string without ``http://``.
    Stay back-compat: extract a known function tail if present, else
    treat the whole string as the base."""
    base, fn = _martin_base_and_default_function("localhost:3000/get_tile")
    assert base == "localhost:3000"
    assert fn == "get_tile"

    base, fn = _martin_base_and_default_function("localhost:3000")
    assert base == "localhost:3000"
    assert fn == "get_tile"
