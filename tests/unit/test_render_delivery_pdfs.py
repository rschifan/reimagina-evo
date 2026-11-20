"""Figures travel inside the delivered PDFs, never beside them.

The HTML is built in a different directory from the figures, and Chromium does
not load a file next to it even with a base href: the PDF comes out typeset,
with a broken-image icon where the figure should be. SVG diagrams were inlined
for that reason; the app screenshots are PNG and must be embedded too.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from scripts.render_delivery_pdfs import _inline_figures

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_a_screenshot_is_embedded_as_data(tmp_path: Path) -> None:
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "page.png").write_bytes(PNG)
    html = _inline_figures(
        '<p><img src="shots/page.png" alt="The page" /></p>', tmp_path
    )
    assert 'src="data:image/png;base64,' in html
    assert "shots/page.png" not in html
    assert 'alt="The page"' in html


def test_a_missing_screenshot_stops_the_render(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing.png"):
        _inline_figures('<img src="missing.png" alt="x">', tmp_path)


def test_diagrams_are_still_inlined(tmp_path: Path) -> None:
    (tmp_path / "d.svg").write_text('<svg width="100" height="100"></svg>')
    assert "<svg" in _inline_figures('<img src="d.svg" alt="d">', tmp_path)
