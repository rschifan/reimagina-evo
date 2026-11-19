"""Tests for the delivery-PDF template's text handling.

Rendering needs pandoc and Chromium, so these cover only the parsing the
template rests on: where the cover's fields come from, and how the contents
page numbers are read back out of a rendered PDF.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_delivery_pdfs as r  # noqa: E402

DOC = """---
title: "Technical documentation"
subtitle: "Reimagina · Project — Schedule A, Work 4"
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
