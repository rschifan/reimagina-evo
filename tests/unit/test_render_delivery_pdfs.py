"""Tests for the delivery-PDF template's text handling.

Rendering needs pandoc and Chromium, so these cover only the parsing the
template rests on: where the cover's fields come from, and how the contents
page numbers are read back out of a rendered PDF.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_delivery_pdfs as r  # noqa: E402

DOC = """---
title: "Technical documentation"
date: "20 November 2026"
---

## Document control

| | |
|---|---|
| **Version** | 1.0 |
| **Deliverable** | Schedule A, Work 4 — Technical documentation |

## 1. Introduction
"""


def test_cover_fields_come_from_the_document() -> None:
    meta, body = r._front_matter(DOC)
    front, main = r._split_front(body)
    assert meta["title"] == "Technical documentation"
    assert r._pdf_date(meta["date"]) == "D:20261120000000+00'00'"
    assert r._control_rows(front) == {
        "Version": "1.0",
        "Deliverable": "Schedule A, Work 4 — Technical documentation",
    }
    assert main.startswith("## 1. Introduction")


def test_contents_pages_follow_the_page_tree_not_object_numbers() -> None:
    # Pages listed out of object order, one nested Pages node, a #-escaped name.
    pdf = (
        b"%PDF-1.4\n"
        b"\n1 0 obj\n<</Type /Pages /Count 3 /Kids [4 0 R 2 0 R]>>\nendobj"
        b"\n2 0 obj\n<</Type /Pages /Count 2 /Kids [5 0 R 3 0 R]>>\nendobj"
        b"\n3 0 obj\n<</Type /Page>>\nendobj"
        b"\n4 0 obj\n<</Type /Page>>\nendobj"
        b"\n5 0 obj\n<</Type /Page>>\nendobj"
        b"\n6 0 obj\n<</intro [4 0 R /XYZ 0 0 0]\n/espa#C3#B1a [3 0 R /XYZ 0 0 0]>>\nendobj"
        b"\n7 0 obj\n<</Type /Catalog\n/Pages 1 0 R\n/Dests 6 0 R>>\nendobj\n"
    )
    assert r._anchor_pages(pdf) == {"intro": 1, "españa": 3}


def test_inline_code_breaks_but_code_blocks_do_not() -> None:
    long = "curated_geo.indicator_result"
    page = r._typeset(f"<p><code>{long}</code></p><pre><code>{long}</code></pre>")
    assert "<p><code>curated_<wbr>geo.<wbr>indicator_<wbr>result</code></p>" in page
    assert f"<pre><code>{long}</code></pre>" in page


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_a_screenshot_is_embedded_as_data(tmp_path: Path) -> None:
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "page.png").write_bytes(PNG)
    html = r._inline_figures(
        '<p><img src="shots/page.png" alt="The page" /></p>', tmp_path
    )
    assert 'src="data:image/png;base64,' in html
    assert "shots/page.png" not in html
    assert 'alt="The page"' in html


def test_a_missing_screenshot_stops_the_render(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing.png"):
        r._inline_figures('<img src="missing.png" alt="x">', tmp_path)


def test_diagrams_are_still_inlined(tmp_path: Path) -> None:
    (tmp_path / "d.svg").write_text('<svg width="100" height="100"></svg>')
    assert "<svg" in r._inline_figures('<img src="d.svg" alt="d">', tmp_path)


def test_source_links_print_portable_paths_instead_of_host_file_urls(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(r, "ROOT", tmp_path)
    source = tmp_path / "docs/delivery"
    source.mkdir(parents=True)
    (tmp_path / "docs/INSTALLATION.md").write_text("Setup")
    page = '<a href="../INSTALLATION.md">Installation</a>'
    rendered = r._portable_links(page, source)
    assert "docs/INSTALLATION.md" in rendered
    assert "href=" not in rendered
    assert str(tmp_path) not in rendered


def test_pdf_internal_and_web_links_remain_clickable(tmp_path):
    page = '<a href="#schemas">Schemas</a><a href="https://example.com">Web</a>'
    assert r._portable_links(page, tmp_path) == page


def test_diagram_and_caption_share_one_unbreakable_figure(tmp_path):
    (tmp_path / "d.svg").write_text('<svg width="100" height="100"></svg>')
    page = (
        '<p><img src="d.svg" alt="Diagram" /></p>\n<p><em>Figure 1 — Diagram.</em></p>'
    )
    rendered = r._inline_figures(page, tmp_path)
    assert rendered.startswith("<figure>")
    assert "<figcaption><em>Figure 1 — Diagram.</em></figcaption>" in rendered
    assert rendered.endswith("</figure>")
    assert "<p><div" not in rendered
