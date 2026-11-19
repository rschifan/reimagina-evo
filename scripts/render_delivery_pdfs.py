"""Render the delivery documents to PDF (Schedule A, Works 1, 3 and 4).

Three of the four Works are accepted as PDFs, and one criterion is simply that
the result is *legible*. So this renders through a pipeline that gives control
over typography rather than whatever a default template produces: pandoc turns
the Markdown into HTML, a print stylesheet sets the page up, and headless
Chromium prints it.

Every document gets the same report template, and everything on it comes from
the document itself — nothing here is written per document:

* a cover: ``title``, the **Deliverable**, **Version** and **Status**
  rows of the document-control table, ``date`` and ``author``. A document
  missing any of them stops the render, naming what is missing;
* the document-control section the document opens with (everything before
  its second level-2 heading), then a table of contents with page numbers,
  then the body, each starting on a new page;
* a running header (title · version) and footer (Schedule A, Work N · page X
  of Y), drawn by CSS page-margin boxes and suppressed on the cover.

Contents page numbers come from rendering twice. Chromium writes every link
target into the PDF as a named destination, so the first render says on which
page each heading landed. The numbers go into slots of fixed width, so the
second render lays out exactly like the first — which is checked, not assumed.

The type is IBM Plex Sans and IBM Plex Mono (SIL OFL 1.1), vendored in
``docs/delivery/fonts`` so the result does not depend on the fonts of the
machine that renders it. The few glyphs Plex lacks (⚠, ⌘, ✕) fall back to a
system font.

The stylesheet does most of the real work. These documents are dense with
long identifiers — index definitions, constraint expressions, DSNs — and the
default failure is a table that runs off the right edge of the page, taking
the text with it. Tables size their columns to their content, inline code
gains break opportunities after ``_ . / , ( =``, code blocks wrap, and
headings are kept with the content beneath them.

Toolchain (nothing here is declared in the repository's own dependencies; the
committed hashes in ``docs/delivery/SHA256SUMS`` were produced with exactly
these, on macOS):

* ``pandoc`` 3.6.3
* Node.js >= 22 (24 was used)
* Playwright 1.64.x with its Chromium (page-margin boxes need Chromium 131+).
  Provide it without touching the lockfile, from ``apps/web``::

      cd apps/web
      npm i --no-save playwright@1.64.1 && npx playwright install chromium
      cd ../..
      export NODE_PATH="$PWD/apps/web/node_modules"

Another pandoc or Playwright version may lay the pages out differently, so the
PDFs would be correct but would not match ``SHA256SUMS`` byte for byte.

Usage::

    python scripts/render_delivery_pdfs.py            # every docs/delivery/NN_*.md
    python scripts/render_delivery_pdfs.py 03_database_and_api_reference.md
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "docs" / "delivery"
BUILD = DELIVERY / "pdf"

#: The numbered Schedule A documents, and only those.
#:
#: This was `*.md`, which was right until `MANIFEST.md` landed in the same
#: directory — the renderer then produced a `MANIFEST.pdf` nobody asked for,
#: carrying the 1970 fallback timestamp because the manifest has no YAML
#: front matter to take a date from. Found by rendering a clean checkout
#: rather than by reading the code, and the fallback is why it was obvious:
#: a plausible-looking date would have shipped unnoticed.
#:
#: A prefix convention rather than an exclusion list, so a generated file
#: added here later is excluded by default instead of by remembering to.
WORK_GLOB = "[0-9][0-9]_*.md"

_ACCENT = "#1f3b5c"
_MUTED = "#5b6472"
_RULE = "#c3c9d2"


def _face(family: str, file: str, weight: int, style: str = "normal") -> str:
    return (
        f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
        f'font-style: {style}; src: url("../fonts/{file}.woff2") format("woff2"); }}'
    )


PRINT_CSS = (
    "\n".join(
        [
            _face("IBM Plex Sans", "IBMPlexSans-Regular", 400),
            _face("IBM Plex Sans", "IBMPlexSans-Italic", 400, "italic"),
            _face("IBM Plex Sans", "IBMPlexSans-SemiBold", 600),
            _face("IBM Plex Sans", "IBMPlexSans-Bold", 700),
            _face("IBM Plex Mono", "IBMPlexMono-Regular", 400),
            _face("IBM Plex Mono", "IBMPlexMono-SemiBold", 600),
        ]
    )
    + f"""
/* A4, 170 x 251 mm text block. The header and footer live in the margins. */
@page {{
  size: A4;
  margin: 24mm 20mm 22mm 20mm;
  @top-left {{ font: 400 7.5pt "IBM Plex Sans"; color: {_MUTED};
              vertical-align: bottom; padding-bottom: 2.5mm;
              border-bottom: .5pt solid {_RULE}; }}
  @top-right {{ content: ""; vertical-align: bottom; padding-bottom: 2.5mm;
               border-bottom: .5pt solid {_RULE}; }}
  @bottom-left {{ font: 400 7.5pt "IBM Plex Sans"; color: {_MUTED};
                 vertical-align: top; padding-top: 2.5mm;
                 border-top: .5pt solid {_RULE}; }}
  @bottom-right {{ content: "Page " counter(page) " of " counter(pages);
                  font: 400 7.5pt "IBM Plex Sans"; color: {_MUTED};
                  text-align: right; vertical-align: top; padding-top: 2.5mm;
                  border-top: .5pt solid {_RULE}; }}
}}
html {{ font-size: 9.75pt; }}
body {{ font-family: "IBM Plex Sans", sans-serif; line-height: 1.5;
       color: #1a1d23; margin: 0; font-kerning: normal; }}
p {{ margin: 0 0 .7em; }}
p, li {{ orphans: 3; widows: 3; }}
/* A lead-in ("Indexes:") stays with the table, code or list it introduces. */
p:has(+ table, + pre, + ul, + ol) {{ break-after: avoid; }}
a {{ color: {_ACCENT}; text-decoration: none; }}
strong {{ font-weight: 600; }}
hr {{ border: none; border-top: .5pt solid {_RULE}; margin: 1.6em 0; }}

/* ── Cover ─────────────────────────────────────────────────────────── */
.cover {{ height: 248mm; display: flex; flex-direction: column;
         border-top: 3pt solid {_ACCENT}; break-after: page; }}
.cover h1 {{ margin: 66mm 0 0; font-size: 27pt; font-weight: 600;
            line-height: 1.15; letter-spacing: -.01em; color: {_ACCENT};
            text-wrap: balance; }}
.cover .deliverable {{ margin: 5mm 0 0; font-size: 12.5pt; color: #3d4654; }}
.cover dl {{ margin: auto 0 0; padding-top: 4mm; display: grid;
            grid-template-columns: 28mm 1fr; row-gap: 1.6mm;
            border-top: .5pt solid {_RULE}; font-size: 9.5pt; }}
.cover dt {{ color: {_MUTED}; }}
.cover dd {{ margin: 0; }}

/* ── Document control, contents ───────────────────────────────────── */
.front {{ break-after: page; font-size: .92em; }}
.front th, .front td {{ padding-top: .26em; padding-bottom: .26em; }}
.front > hr:last-child {{ display: none; }}
.front > h2:first-child, nav > h2 {{ margin-top: 0; }}
nav {{ break-after: page; }}
nav ul {{ list-style: none; margin: 0; padding: 0; }}
nav > ul > li {{ margin-top: .7em; font-weight: 600; }}
nav li li {{ font-weight: 400; padding-left: 2.6em; margin-top: .15em; }}
nav li li li {{ padding-left: 2.6em; }}
nav a {{ display: flex; align-items: baseline; color: inherit; }}
nav .dots {{ flex: 1 1 auto; min-width: 1.5em; margin: 0 .4em;
            border-bottom: .8pt dotted #9aa4b2; }}
nav .pg {{ flex: 0 0 2.2em; text-align: right;
          font-variant-numeric: tabular-nums; }}
nav .secnum {{ min-width: 2.6em; }}

/* ── Headings: the documents number their own; the number hangs. ───── */
h2, h3, h4, h5 {{ break-after: avoid; break-inside: avoid; color: {_ACCENT};
             font-weight: 600; line-height: 1.3; text-wrap: balance; }}
h2 {{ font-size: 15pt; margin: 1.7em 0 .65em; padding-bottom: .3em;
     border-bottom: .75pt solid {_ACCENT}; }}
h3 {{ font-size: 12pt; margin: 1.45em 0 .5em; }}
h4 {{ font-size: 10.25pt; margin: 1.3em 0 .45em; color: #1a1d23; }}
h5 {{ font-size: 9.75pt; margin: 1.15em 0 .35em; color: #1a1d23; }}
h4 code, h5 code {{ background: none; padding: 0; font-size: .95em; font-weight: 600; }}
.secnum {{ display: inline-block; min-width: 2.2em; }}
h3 .secnum, h4 .secnum {{ min-width: 2.8em; }}

/* ── Code ──────────────────────────────────────────────────────────── */
code, pre {{ font-family: "IBM Plex Mono", monospace; }}
code {{ font-size: .9em; background: #eff2f5; padding: .05em .28em;
       border-radius: 2px; overflow-wrap: break-word; }}
pre {{ font-size: 8.25pt; line-height: 1.45; margin: .5em 0 1em;
      background: #f5f7f9; border: .5pt solid #dde2e8;
      border-left: 2.5pt solid {_ACCENT}; padding: .6em .85em;
      white-space: pre-wrap; overflow-wrap: anywhere; }}
pre code {{ background: none; padding: 0; font-size: inherit; }}

/* ── Notes and warnings ────────────────────────────────────────────── */
blockquote {{ margin: .8em 0 1em; padding: .55em .9em; background: #f3f5f8;
             border-left: 2.5pt solid #8a96a8; break-inside: avoid; }}
blockquote p {{ margin: .25em 0; }}
p.warning {{ padding: .55em .9em; background: #fbf5e6;
            border-left: 2.5pt solid #b7791f; break-inside: avoid; }}

/* ── Lists ─────────────────────────────────────────────────────────── */
ul, ol {{ margin: .3em 0 .85em; padding-left: 1.5em; }}
li {{ margin: .18em 0; }}
li > p {{ margin: 0 0 .3em; }}

/* ── Tables: columns sized to content, shaded header, row rules ────── */
table {{ border-collapse: collapse; width: 100%; table-layout: auto;
        margin: .6em 0 1.3em; font-size: 8.25pt; line-height: 1.4; }}
thead {{ display: table-header-group; }}
th {{ background: #e8ecf1; font-weight: 600; text-align: left;
     border-bottom: .9pt solid #8a96a8; }}
th, td {{ padding: .38em .6em; vertical-align: top;
         overflow-wrap: break-word; }}
td {{ border-bottom: .5pt solid #dde2e8; }}
td.nw {{ white-space: nowrap; }}
tr {{ break-inside: avoid; }}
tbody tr:last-child {{ break-before: avoid; }}  /* no lone last row */
td code, th code {{ font-size: .95em; padding: 0 .2em; }}
/* A table with no header row is a key/value table (document control). */
table:not(:has(thead)) {{ border-top: .9pt solid #8a96a8; }}
table:not(:has(thead)) td:first-child {{ width: 26%; background: #f3f5f8; }}

/* ── Figures: the caption is part of the SVG, so none is added. ────── */
.diagram {{ break-inside: avoid; text-align: center; margin: 1em 0 1.4em; }}
.diagram svg {{ width: 100%; height: auto; max-height: 240mm; }}
"""
)

#: Per document: the running header and footer text, then their suppression
#: on the cover — which has to come after, or this rule wins over it.
_PAGE_CSS = """@page {{
  @top-left {{ content: "{header}"; }}
  @bottom-left {{ content: "{footer}"; }}
}}
@page :first {{
  @top-left {{ content: none; border: none; }}
  @top-right {{ content: none; border: none; }}
  @bottom-left {{ content: none; border: none; }}
  @bottom-right {{ content: none; border: none; }}
}}"""

#: pandoc writes the contents and the body; the marker splits them.
_TEMPLATE = "$table-of-contents$\n<!--body-->\n$body$\n"

RENDER_JS = """
const {chromium} = require('playwright');
const [html, pdf] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + html, {waitUntil: 'load'});
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({
    path: pdf, printBackground: true, preferCSSPageSize: true, outline: true,
  });
  await browser.close();
})().catch((err) => { console.error(err); process.exit(1); });
"""


def _inline_svg(page: str, assets: Path) -> str:
    """Replace `<img src="*.svg">` with the SVG itself.

    Referencing the file and letting the browser fetch it does not survive the
    trip: the HTML is built in a different directory from the diagrams, and
    Chromium would not load the file even with a correct base href — the page
    rendered a broken-image icon and the PDF came out looking perfectly
    typeset with nothing where each diagram should be. That is the worst kind
    of defect in a delivered document, because it does not announce itself.

    Inlining removes the question. The PDF carries the vector artwork itself
    and depends on no file beside it. The figure carries its own caption, so
    the image's alt text is not repeated under it.
    """

    def replace(match: re.Match[str]) -> str:
        src = match.group("src")
        if not src.endswith(".svg"):
            return match.group(0)
        path = (assets / src).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"diagram referenced but missing: {src}")
        return f'<div class="diagram">{path.read_text()}</div>'

    return re.sub(r'<img[^>]*src="(?P<src>[^"]+)"[^>]*/?>', replace, page)


def _front_matter(markdown: str) -> tuple[dict[str, str], str]:
    """The YAML front-matter block's `key: value` lines, and the rest."""
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---", 4)
    meta = {}
    for line in markdown[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, markdown[markdown.index("\n", end + 1) + 1 :]


def _split_front(body: str) -> tuple[str, str]:
    """The document-control section, and everything after it.

    The front part ends at the second level-2 heading: the first is the
    document's own "Document control".
    """
    starts = [m.start() for m in re.finditer(r"^## ", body, re.M)]
    if len(starts) < 2:
        return "", body
    return body[: starts[1]], body[starts[1] :]


def _control_rows(front: str) -> dict[str, str]:
    """`| **Key** | value |` rows of the document-control table."""
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.*?)\s*\|\s*$", front, re.M)
    }


_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january february march april may june july "
            "august september october november december"
        ).split(),
        start=1,
    )
}

#: Used when a document declares no parseable date. A fixed value keeps the
#: build reproducible rather than falling back to the clock, which is the
#: whole point; it is deliberately not a plausible delivery date, so a PDF
#: carrying it is visibly missing its `date:` rather than quietly wrong.
_FALLBACK_STAMP = "D:19700101000000+00'00'"


def _pdf_date(declared: str) -> str:
    """A `date:` such as "20 November 2026", as a PDF timestamp.

    Returns a fixed-width 23-byte payload, which is what makes the
    substitution in `_pin_timestamps` safe — see that function.
    """
    parts = declared.split()
    if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
        number = _MONTHS.get(parts[1].lower())
        if number is not None:
            return f"D:{int(parts[2]):04d}{number:02d}{int(parts[0]):02d}000000+00'00'"
    return _FALLBACK_STAMP


def _pin_timestamps(pdf: Path, stamp: str) -> None:
    """Rewrite the PDF's CreationDate and ModDate to a deterministic value.

    Chromium stamps the wall clock into both, and they are the *only*
    non-determinism in this pipeline: rendering the same Markdown twice
    differs in exactly 4 bytes of 215,094, all inside these two fields. Every
    glyph, every inlined SVG and the xref table are already byte-identical.

    Pinning them turns the delivery from "trust these bytes" into "rebuild
    and compare", which needs no trust at all — and Article 2.2 of the
    agreement identifies a delivery by its SHA-256, so a hash a recipient can
    reproduce is worth more than one they can only check against itself.

    ⚠ **The replacement must be the same length as what it replaces**, and
    the regex below enforces it by matching a fixed-width payload. A PDF's
    xref table stores absolute byte offsets; a 22- or 24-byte value for a
    23-byte field invalidates every offset after it, producing a file some
    readers silently repair and others reject. The timestamp format is
    fixed-width, so this costs nothing — but it is the line that must not be
    "tidied" into a general substitution later.
    """
    raw = pdf.read_bytes()
    want = stamp.encode()
    pattern = re.compile(rb"(/(?:CreationDate|ModDate)\s*\()D:\d{14}\+\d{2}'\d{2}'(\))")

    def replace(match: "re.Match[bytes]") -> bytes:
        return match.group(1) + want + match.group(2)

    patched, count = pattern.subn(replace, raw)
    if count == 0:
        # Not fatal: a future renderer may emit no Info dictionary at all,
        # which is reproducible by construction. Silence would be wrong
        # though — this is the one step the manifest's promise rests on.
        print(f"  note: {pdf.name} carries no timestamps to pin", file=sys.stderr)
        return
    if len(patched) != len(raw):  # pragma: no cover - the regex forbids it
        raise AssertionError(
            f"{pdf.name}: pinning changed the file length by "
            f"{len(patched) - len(raw)} bytes, which invalidates the xref table"
        )
    pdf.write_bytes(patched)


def _anchor_pages(pdf: bytes) -> dict[str, int]:
    """Page number (1-based) of every named destination in a Chromium PDF.

    Chromium (Skia) writes an uncompressed `/Dests` dictionary mapping each
    link target's id to `[page 0 R /XYZ …]`, and an uncompressed page tree.
    """

    def obj(number: int) -> bytes:
        start = pdf.index(b"\n%d 0 obj" % number)
        return pdf[start : pdf.index(b"endobj", start)]

    catalog = re.search(rb"/Type /Catalog\s*/Pages (\d+) 0 R\s*/Dests (\d+) 0 R", pdf)
    if catalog is None:
        raise RuntimeError("no /Dests in the rendered PDF: cannot number the contents")
    order: list[int] = []

    def walk(number: int) -> None:
        node = obj(number)
        if b"/Type /Pages" in node:
            kids = re.search(rb"/Kids \[([^\]]*)\]", node)
            for kid in re.findall(rb"(\d+) 0 R", kids.group(1) if kids else b""):
                walk(int(kid))
        else:
            order.append(number)

    walk(int(catalog.group(1)))
    page_of = {number: index for index, number in enumerate(order, start=1)}

    def name(raw: bytes) -> str:
        return re.sub(
            rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw
        ).decode()

    return {
        name(m.group(1)): page_of[int(m.group(2))]
        for m in re.finditer(rb"/(\S+) \[(\d+) 0 R", obj(int(catalog.group(2))))
    }


def _pandoc(markdown: str, *args: str) -> str:
    return subprocess.run(
        ["pandoc", "--from=gfm", "--to=html5", "--no-highlight", "--wrap=none", *args],
        input=markdown,
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout


def _typeset(page: str) -> str:
    """Small HTML refinements pandoc's GFM writer has no option for."""
    # Break opportunities in long inline code (never inside a code block).
    parts = re.split(r"(<pre[\s\S]*?</pre>)", page)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(
            r"<code>([^<]{24,})</code>",
            lambda m: (
                "<code>"
                + re.sub(r"([_./,(=])(?=\S)", r"\1<wbr>", m.group(1))
                + "</code>"
            ),
            parts[i],
        )
    page = "".join(parts)
    # The documents number their own headings; set the number apart.
    page = re.sub(
        r'(<h[2-5] id="[^"]*">|<span class="t">)(\d+(?:\.\d+)*\.?) ',
        r'\1<span class="secnum">\2</span>',
        page,
    )
    # A short cell (a version, a date, a name) never wraps: left to the
    # automatic layout, a long neighbouring column squeezes it to "2026-/11-08".
    page = re.sub(
        r"<td([^>]*)>(.*?)</td>",
        lambda m: (
            m.group(0)
            if len(re.sub(r"<[^>]+>", "", m.group(2))) > 20
            else f'<td class="nw"{m.group(1)}>{m.group(2)}</td>'
        ),
        page,
    )
    return page.replace("<p>⚠", '<p class="warning">⚠')


def _contents(toc: str, pages: dict[str, int]) -> str:
    """Pandoc's contents list, each entry with its page in a fixed-width slot."""

    def entry(m: re.Match[str]) -> str:
        page = pages.get(m.group(1), "")
        return (
            f'{m.group(0)[: m.group(0).index(">") + 1]}<span class="t">{m.group(2)}'
            f'</span><span class="dots"></span><span class="pg">{page}</span></a>'
        )

    return re.sub(r'<a href="#([^"]+)" id="toc-[^"]*">(.*?)</a>', entry, toc)


def render(source: Path, css: Path, js: Path, template: Path) -> Path:
    page_html = BUILD / (source.stem + ".html")
    pdf = BUILD / (source.stem + ".pdf")
    meta, body = _front_matter(source.read_text())
    front, main = _split_front(body)
    rows = _control_rows(front)

    wanted = {
        "title": meta.get("title"),
        "author": meta.get("author"),
        "date": meta.get("date"),
        "Deliverable": rows.get("Deliverable"),
        "Version": rows.get("Version"),
        "Status": rows.get("Status"),
    }
    if missing := [key for key, value in wanted.items() if not value]:
        raise SystemExit(
            f"{source.name}: the cover needs {', '.join(missing)} "
            "(front matter, or a **row** of the document-control table)"
        )
    title, version = meta["title"], rows["Version"]
    work = rows["Deliverable"].split(" — ")[0]

    toc, main_html = _pandoc(
        main,
        "--toc",
        "--toc-depth=3",
        f"--template={template}",
        "--metadata",
        f"pagetitle={title}",
    ).split("<!--body-->", 1)

    def quoted(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    cover = (
        f'<section class="cover"><h1>{escape(title)}</h1>'
        f'<p class="deliverable">{escape(rows["Deliverable"])}</p><dl>'
        + "".join(
            f"<dt>{label}</dt><dd>{escape(value)}</dd>"
            for label, value in (
                ("Version", version),
                ("Date", meta["date"]),
                ("Author", meta["author"]),
                ("Status", rows["Status"]),
            )
        )
        + "</dl></section>"
    )
    page_css = _PAGE_CSS.format(
        header=quoted(f"{title} · Version {version}"), footer=quoted(work)
    )

    def write(pages: dict[str, int]) -> None:
        document = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f"<title>{escape(title)}</title>\n"
            f'<link rel="stylesheet" href="{css.name}">\n'
            f"<style>\n{page_css}\n</style>\n</head>\n<body>\n{cover}\n"
            f'<section class="front">\n{_pandoc(front)}</section>\n'
            f"<nav><h2>Contents</h2>\n{_contents(toc, pages)}</nav>\n"
            f"<main>\n{main_html}</main>\n</body>\n</html>\n"
        )
        page_html.write_text(_typeset(_inline_svg(document, source.parent)))
        subprocess.run(
            ["node", str(js), str(page_html), str(pdf)], check=True, cwd=ROOT
        )

    # Two passes: the first finds the pages, the second prints them.
    write({})
    pages = _anchor_pages(pdf.read_bytes())
    write(pages)
    linked = re.findall(r'<a href="#([^"]+)" id="toc-', toc)
    after = _anchor_pages(pdf.read_bytes())
    if [after.get(i) for i in linked] != [pages.get(i) for i in linked]:
        raise RuntimeError(f"{source.name}: contents page numbers moved between passes")
    if unplaced := [i for i in linked if i not in pages]:
        raise RuntimeError(f"{source.name}: contents entries not found: {unplaced}")

    _pin_timestamps(pdf, _pdf_date(meta["date"]))
    return pdf


def _missing_tools() -> list[str]:
    """What the pipeline needs and cannot find, each with how to provide it."""
    missing = []
    if shutil.which("pandoc") is None:
        missing.append("pandoc 3.6.3 (https://pandoc.org/installing.html)")
    if shutil.which("node") is None:
        missing.append("Node.js >= 22")
    elif (
        subprocess.run(
            ["node", "-e", "require.resolve('playwright')"], capture_output=True
        ).returncode
        != 0
    ):
        missing.append(
            "Playwright 1.64.x with Chromium: in apps/web run "
            "`npm i --no-save playwright@1.64.1 && npx playwright install chromium`, "
            'then `export NODE_PATH="$PWD/apps/web/node_modules"`'
        )
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render delivery docs to PDF.")
    parser.add_argument("names", nargs="*", help="Markdown files; default all.")
    args = parser.parse_args(argv)

    sources = (
        [DELIVERY / n for n in args.names]
        if args.names
        else sorted(DELIVERY.glob(WORK_GLOB))
    )
    missing = [s for s in sources if not s.is_file()]
    if missing:
        print(f"not found: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        return 2
    if not sources:
        print(f"no Markdown in {DELIVERY.relative_to(ROOT)}", file=sys.stderr)
        return 2

    # Chromium falls back to a system font without a word, so a missing font
    # file would change every page and every hash silently.
    fonts = re.findall(r'url\("\.\./fonts/([^"]+)"\)', PRINT_CSS)
    if absent := [f for f in fonts if not (DELIVERY / "fonts" / f).is_file()]:
        print(f"fonts missing from docs/delivery/fonts: {absent}", file=sys.stderr)
        return 2

    if missing_tools := _missing_tools():
        print(
            "cannot render; missing:\n  - " + "\n  - ".join(missing_tools),
            file=sys.stderr,
        )
        return 2

    BUILD.mkdir(parents=True, exist_ok=True)
    css = BUILD / "print.css"
    css.write_text(PRINT_CSS)
    js = BUILD / "_render.js"
    js.write_text(RENDER_JS)
    template = BUILD / "_template.html"
    template.write_text(_TEMPLATE)

    for source in sources:
        pdf = render(source, css, js, template)
        print(
            f"  {source.name} -> {pdf.relative_to(ROOT)} "
            f"({pdf.stat().st_size // 1024} KB)"
        )
    js.unlink()
    template.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
