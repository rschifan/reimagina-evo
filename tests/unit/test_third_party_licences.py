"""Tests for the licence inventory generator.

``docs/THIRD_PARTY_LICENSES.md`` is not documentation in the ordinary sense.
It is the artefact that answers the delivery obligation to hand over a
complete list of third-party and open-source code with each item's licence
terms, and to warrant that nothing in it materially restricts the recipient's
rights. The number that warrant rests on is the "strong copyleft" count in the
generated summary.

So the classifier is a contractual surface, and it was shipped with no tests
at all. The two cases below are the ones that were actually wrong, and both
failed *silently* — producing a confident, well-formatted, incorrect
statement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import third_party_licences as tpl  # noqa: E402

# Opening lines of the real licences, verbatim. Shortened, not paraphrased:
# the bug these catch lived in the exact wording.
GPL3 = """                    GNU GENERAL PUBLIC LICENSE
                       Version 3, 29 June 2007

 Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>
 Everyone is permitted to copy and distribute verbatim copies
 of this license document, but changing it is not allowed.
"""

LGPL3 = """                   GNU LESSER GENERAL PUBLIC LICENSE
                       Version 3, 29 June 2007

 Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>
 Everyone is permitted to copy and distribute verbatim copies
 of this license document, but changing it is not allowed.
"""

AGPL3 = """                    GNU AFFERO GENERAL PUBLIC LICENSE
                       Version 3, 19 November 2007

 Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>
 Everyone is permitted to copy and distribute verbatim copies
"""

MIT = """MIT License

Copyright (c) 2024 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction.
"""

APACHE2 = """                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/
"""

BSD3 = """BSD 3-Clause License

Copyright (c) 2024, Someone
All rights reserved.
"""

MPL2 = """Mozilla Public License Version 2.0
==================================
"""

OPAQUE = """This software is distributed under our standard commercial terms.
Contact legal@example.com for a copy.
"""


class TestClassify:
    """``classify`` over the strings the rest of the script actually produces."""

    @pytest.mark.parametrize(
        ("text", "bucket"),
        [
            # Versioned SPDX identifiers, which came off a manifest.
            ("GPL-3.0-only", tpl.STRONG),
            ("AGPL-3.0", tpl.STRONG),
            ("GPLv2", tpl.STRONG),
            ("LGPL-2.1-or-later", tpl.WEAK),
            ("MPL-2.0", tpl.WEAK),
            ("MIT", tpl.PERM),
            ("Apache-2.0", tpl.PERM),
            ("BSD-3-Clause", tpl.PERM),
            # ⚠ The regression. `_licence_from_file` emits a *bare* family
            # name when it reads one out of a file, and an unversioned "GPL"
            # matched none of `\bGPL-`, `\bGPLv` or `GNU General Public`. It
            # landed in `undeclared`, so a GPL dependency was absent from the
            # one count the delivery warranty depends on.
            ("GPL", tpl.STRONG),
            ("GPL (per LICENSE; not declared in manifest)", tpl.STRONG),
            # The same alternative must not swallow LGPL — there is no word
            # boundary before the G, and weak copyleft is tested first anyway.
            ("LGPL", tpl.WEAK),
            ("LGPL (per COPYING; not declared in manifest)", tpl.WEAK),
            ("", tpl.UNDECLARED),
            ("see LICENSE", tpl.UNDECLARED),
        ],
    )
    def test_buckets(self, text: str, bucket: str) -> None:
        assert tpl.classify(text) == bucket


class TestLicenceFromFile:
    """``_licence_from_file`` over verbatim licence text."""

    @staticmethod
    def _read(tmp_path: Path, body: str, name: str = "LICENSE") -> tuple[str, str]:
        (tmp_path / name).write_text(body, encoding="utf-8")
        return tpl._licence_from_file(tmp_path)

    @pytest.mark.parametrize(
        ("body", "bucket"),
        [
            # ⚠ The bug that mattered most. The scan was
            # `if name.lower() in head.lower()` over
            # `("MIT", "Apache", "BSD", "ISC", "MPL", "LGPL", "GPL")`, and the
            # GPL preamble contains "Everyone is per-MIT-ted to copy". So a
            # verbatim GPL-3.0 file was reported as **MIT, permissive**: the
            # strongest copyleft licence recorded as the most permissive one,
            # in the document whose only job is to state that correctly.
            # `COPYING` in a GNU project is the likeliest file to hit it.
            (GPL3, tpl.STRONG),
            (AGPL3, tpl.STRONG),
            (LGPL3, tpl.WEAK),
            (MPL2, tpl.WEAK),
            (MIT, tpl.PERM),
            (APACHE2, tpl.PERM),
            (BSD3, tpl.PERM),
            # No guessing. An unrecognised file is `undeclared`, which the
            # summary reports as needing a human, rather than a plausible
            # bucket nobody will revisit.
            (OPAQUE, tpl.UNDECLARED),
        ],
    )
    def test_file_text_is_classified_correctly(
        self, tmp_path: Path, body: str, bucket: str
    ) -> None:
        licence, source = self._read(tmp_path, body)
        assert source == "file"
        assert tpl.classify(licence) == bucket, licence

    @pytest.mark.parametrize("name", ["LICENSE", "LICENCE", "COPYING", "COPYING.txt"])
    def test_every_conventional_filename_is_read(
        self, tmp_path: Path, name: str
    ) -> None:
        """`COPYING` is the GNU convention, so it must not be the one missed."""
        licence, source = self._read(tmp_path, GPL3, name=name)
        assert source == "file"
        assert tpl.classify(licence) == tpl.STRONG
        assert name in licence  # the reader says which file it read

    def test_no_licence_file_is_not_a_guess(self, tmp_path: Path) -> None:
        assert tpl._licence_from_file(tmp_path) == ("", "")

    def test_directory_named_like_a_licence_is_skipped(self, tmp_path: Path) -> None:
        """Some packages ship a `LICENSES/` directory rather than a file."""
        (tmp_path / "LICENSES").mkdir()
        assert tpl._licence_from_file(tmp_path) == ("", "")


class TestTableCells:
    """A licence string has to survive being put in a markdown table."""

    def test_full_licence_text_in_the_metadata_field_stays_one_row(self) -> None:
        """``License:`` is free text, and some packages put the whole licence in it.

        ⚠ `exactextract` 0.3.0 does exactly this: its `License:` value is the
        entire Apache-2.0 document. The truncation this covers was `raw[:77]`,
        which is right about length and blind to newlines — so one table row
        became three, two of them rendering as stray prose inside the
        inventory and the package's own row losing its last two columns.
        """
        apache = (
            "                                 Apache License\n"
            "                           Version 2.0, January 2004\n"
            "                        http://www.apache.org/licenses/\n"
        )
        cell = tpl._cell(apache)
        assert "\n" not in cell
        assert cell.startswith("Apache License Version 2.0")
        # Still classified from the collapsed text, not merely tidied.
        assert tpl.classify(cell) == tpl.PERM

    def test_pipe_is_escaped(self) -> None:
        """An unescaped pipe shifts every later column by one, silently."""
        assert "\\|" in tpl._cell("MIT | Apache-2.0")
        assert tpl._cell("MIT | Apache-2.0").count("|") == 1

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("MIT", "MIT"),
            ("  MIT  ", "MIT"),
            ("MIT\nLicense", "MIT License"),
            ("MIT\t\tLicense", "MIT License"),
        ],
    )
    def test_short_values_are_untouched_apart_from_whitespace(
        self, raw: str, expected: str
    ) -> None:
        assert tpl._one_line(raw) == expected

    def test_truncation_counts_real_text_not_newlines(self) -> None:
        """Collapsing before truncating is why this is not `raw[:77]`.

        A value padded with newlines would otherwise spend its whole budget on
        whitespace and report almost none of the licence.
        """
        padded = "\n".join(["Apache License"] + [""] * 40 + ["Version 2.0"])
        assert tpl._one_line(padded) == "Apache License Version 2.0"
