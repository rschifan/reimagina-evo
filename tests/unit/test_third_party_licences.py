"""Tests for the licence inventory generator.

``docs/THIRD_PARTY_LICENSES.md`` answers the delivery obligation to list every
third-party component with its licence terms, and the copyleft count in its
summary is the number a reader acts on. So the classifier and the lockfile
resolution are a contractual surface: each case below is a way the document
could be confidently and silently wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import third_party_licences as tpl  # noqa: E402


@pytest.mark.parametrize(
    ("spdx", "bucket"),
    [
        ("GPL-2.0-or-later", tpl.STRONG),
        ("GPL-3.0-or-later WITH GCC-exception-3.1", tpl.STRONG),
        ("AGPL-3.0-only", tpl.STRONG),
        # `\bGPL-` must not fire inside "LGPL-": there is no word boundary.
        ("LGPL-3.0-only", tpl.WEAK),
        ("LGPL-2.1-or-later", tpl.WEAK),
        ("MPL-2.0", tpl.WEAK),
        ("MIT", tpl.PERM),
        ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", tpl.PERM),
        # A dual licence with a permissive choice is permissive (zstd).
        ("BSD-3-Clause OR GPL-2.0-only", tpl.PERM),
        ("MIT OR Apache-2.0", tpl.PERM),
    ],
)
def test_classify(spdx: str, bucket: str) -> None:
    assert tpl.classify(spdx) == bucket


class TestNormalise:
    def test_family_classifier_is_never_guessed(self) -> None:
        """A "BSD License" classifier names no variant: an unlisted package must stop."""
        with pytest.raises(SystemExit, match="PACKAGE_LICENCE"):
            tpl._normalise("some-new-package", "BSD License", "python")

    def test_unknown_identifier_stops_the_run(self) -> None:
        with pytest.raises(SystemExit):
            tpl._normalise("x", "SSPL-1.0", "npm")

    def test_raw_declaration_is_kept_when_normalised(self) -> None:
        assert tpl._normalise(
            "certifi", "Mozilla Public License 2.0 (MPL 2.0)", "python"
        ) == (
            "MPL-2.0",
            "Mozilla Public License 2.0 (MPL 2.0)",
        )

    def test_full_licence_text_becomes_one_short_cell(self) -> None:
        """`exactextract` 0.3.0 puts the whole Apache-2.0 text in `License:`."""
        text = "Apache License\n   Version 2.0, January 2004\n" + "x " * 500
        spdx, raw = tpl._normalise("exactextract", text, "python")
        assert spdx == "Apache-2.0"
        assert "\n" not in raw and raw.endswith("...")

    def test_pipe_is_escaped(self) -> None:
        """An unescaped pipe shifts every later column by one, silently."""
        assert tpl._cell("MIT | Apache-2.0").count("|") == 1


class TestNpmPlatform:
    """The document must not depend on the machine that generates it."""

    @pytest.mark.parametrize(
        ("pkg", "installed"),
        [
            ({}, True),
            ({"os": ["linux"], "cpu": ["x64"], "libc": ["glibc"]}, True),
            ({"os": ["linux"], "cpu": ["x64"], "libc": ["musl"]}, False),
            # npm installs both when the lockfile records no libc.
            ({"os": ["linux"], "cpu": ["x64"]}, True),
            ({"os": ["darwin"]}, False),
            ({"cpu": ["wasm32"]}, False),
            ({"os": ["!win32"]}, True),
        ],
    )
    def test_platform_filter(self, pkg: dict, installed: bool) -> None:
        assert tpl._npm_platform_ok(pkg) is installed


def test_python_markers_resolve_for_linux_not_this_host() -> None:
    lock = {
        "package": [
            {"name": "root", "version": "0", "source": {"editable": "."}},
            {"name": "linuxonly", "version": "1"},
            {"name": "winonly", "version": "1"},
        ]
    }
    roots = [
        {"name": "linuxonly", "marker": "sys_platform == 'linux'"},
        {"name": "winonly", "marker": "sys_platform == 'win32'"},
    ]
    assert tpl._uv_closure(lock, roots) == {"linuxonly": "1"}
