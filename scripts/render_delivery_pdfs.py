"""Render the delivery documents to PDF (Schedule A, Works 1, 3 and 4).

Three of the four Works are accepted as PDFs, and one criterion is simply that
the result is *legible*. So this renders through a pipeline that gives control
over typography rather than whatever a default template produces: pandoc turns
the Markdown into HTML, a print stylesheet sets the page up, and headless
Chromium prints it.

The stylesheet does most of the real work. These documents are dense with
long identifiers — index definitions, constraint expressions, DSNs — and the
default failure is a table that runs off the right edge of the page, taking
the text with it. Code wraps rather than overflows, tables are laid out to the
page width, and headings are kept with the content beneath them.

Chromium and Playwright are already installed; nothing is downloaded.

Usage::

    python scripts/render_delivery_pdfs.py            # every docs/delivery/*.md
    python scripts/render_delivery_pdfs.py 03_database_and_api_reference.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "docs" / "delivery"
BUILD = DELIVERY / "pdf"

PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
html { font-size: 10.5pt; }
body {
  font-family: "DejaVu Sans", "Helvetica Neue", Arial, sans-serif;
  line-height: 1.45; color: #16181d; margin: 0;
}
h1 { font-size: 1.9rem; margin: 0 0 .4em; letter-spacing: -.01em; }
/* Cover page: the title block gets a page of its own. */
#title-block-header { break-after: page; padding-top: 58mm; }
#title-block-header .title { font-size: 2.5rem; line-height: 1.15;
                             margin: 0 0 .35em; letter-spacing: -.015em; }
#title-block-header .subtitle { font-size: 1.15rem; font-weight: 400;
                                color: #454c59; margin: 0 0 2.6em;
                                padding-bottom: 1.1em;
                                border-bottom: 2px solid #16181d; }
#title-block-header .author, #title-block-header .date {
    font-size: .95rem; color: #454c59; margin: .2em 0; }
#TOC { break-after: page; }
#TOC > ul { padding-left: 1.1em; }
h2 { font-size: 1.35rem; margin: 1.6em 0 .5em; padding-bottom: .22em;
     border-bottom: 1.5px solid #c9ced6; break-after: avoid; }
h3 { font-size: 1.12rem; margin: 1.3em 0 .4em; break-after: avoid; }
h4 { font-size: 1.02rem; margin: 1.5em 0 .45em; color: #16181d;
     break-after: avoid; border-left: 3px solid #8d97a6; padding-left: .5em; }
h4 code { background: none; padding: 0; font-size: .95em; font-weight: 600; }
p, li { orphans: 3; widows: 3; }
code, pre {
  font-family: "DejaVu Sans Mono", "SFMono-Regular", Consolas, monospace;
  font-size: .84em;
}
code { background: #f3f4f7; padding: .08em .3em; border-radius: 3px;
       overflow-wrap: anywhere; word-break: break-word; }
pre { background: #f6f7f9; padding: .7em .9em; border-radius: 5px;
      border: 1px solid #e3e6ea; white-space: pre-wrap; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; }
blockquote { margin: 1em 0; padding: .6em 1em; background: #f5f7fa;
             border-left: 3px solid #8d97a6; }
blockquote p { margin: .3em 0; }
table { border-collapse: collapse; width: 100%; margin: .7em 0 1.1em;
        font-size: .8rem; table-layout: fixed; }
th, td { border: 1px solid #d5d9e0; padding: .32em .5em; text-align: left;
         vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
th { background: #eef1f5; font-weight: 600; }
tr { break-inside: avoid; }
a { color: #1c4fd8; text-decoration: none; }
.diagram { break-inside: avoid; text-align: center; margin: 1.2em 0; }
.diagram svg { max-width: 100%; max-height: 222mm; height: auto; }
.plate { break-before: page; break-after: page; position: relative;
         height: 247mm; width: 100%; overflow: visible; }
.plate > svg { position: absolute; top: 0; left: 0;
               transform-origin: top left; transform: translateX(178mm) rotate(90deg);
               max-width: 247mm; max-height: 178mm; height: auto; }
hr { border: none; border-top: 1px solid #d5d9e0; margin: 1.6em 0; }
"""

RENDER_JS = """
const {chromium} = require('playwright');
const [html, pdf, footer] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + html, {waitUntil: 'load'});
  await page.pdf({
    path: pdf,
    format: 'A4',
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate:
      '<div style="width:100%;font-size:8px;color:#6b7280;' +
      'padding:0 16mm;display:flex;justify-content:space-between;">' +
      '<span>' + footer + '</span>' +
      '<span><span class="pageNumber"></span> / ' +
      '<span class="totalPages"></span></span></div>',
    margin: {top: '18mm', bottom: '20mm', left: '16mm', right: '16mm'},
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
    and depends on no file beside it.
    """

    def replace(match: re.Match[str]) -> str:
        src = match.group("src")
        if not src.endswith(".svg"):
            return match.group(0)
        path = (assets / src).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"diagram referenced but missing: {src}")
        svg = path.read_text()
        size = re.search(r'<svg[^>]*\swidth="(\d+)"\s+height="(\d+)"', svg)
        wide = size is not None and int(size.group(1)) / int(size.group(2)) > 1.4
        # A figure much wider than tall is unreadable scaled to a portrait
        # column, so it gets a landscape plate: its own page, turned ninety
        # degrees. This is ordinary practice for dense figures in engineering
        # documents, and it roughly halves the scale-down — measured at 7.6pt
        # against 5.4pt for the container diagram.
        return f'<div class="{"plate" if wide else "diagram"}">{svg}</div>'

    return re.sub(r'<img[^>]*src="(?P<src>[^"]+)"[^>]*/?>', replace, page)


def _front_matter_title(markdown: str) -> str:
    """Read `title:` out of a YAML front-matter block."""
    end = markdown.find("\n---", 4)
    for line in markdown[4 : end if end > 0 else len(markdown)].splitlines():
        if line.startswith("title:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _split_title(markdown: str) -> tuple[str, str]:
    """Take the document's own H1 as the title and drop it from the body.

    Otherwise the title appears twice — once in pandoc's title block, built
    from the filename, and once as the heading the document actually carries.
    Lifting the real H1 gives the conventional report order: title, contents,
    body.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[:index] + lines[index + 1 :])
    return "", markdown


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


def _front_matter_date(markdown: str) -> str:
    """The document's declared `date:`, as a PDF timestamp.

    Returns a fixed-width 23-byte payload, which is what makes the
    substitution in `_pin_timestamps` safe — see that function.
    """
    end = markdown.find("\n---", 4)
    for line in markdown[4 : end if end > 0 else len(markdown)].splitlines():
        if not line.startswith("date:"):
            continue
        parts = line.split(":", 1)[1].strip().strip('"').strip("'").split()
        if len(parts) != 3:
            break
        day, month, year = parts
        if not (day.isdigit() and year.isdigit()):
            break
        number = _MONTHS.get(month.lower())
        if number is None:
            break
        return f"D:{int(year):04d}{number:02d}{int(day):02d}000000+00'00'"
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


def render(source: Path, css: Path, js: Path) -> Path:
    html = BUILD / (source.stem + ".html")
    pdf = BUILD / (source.stem + ".pdf")
    raw = source.read_text()
    has_front_matter = raw.startswith("---\n")
    if has_front_matter:
        title = _front_matter_title(raw) or source.stem.replace("_", " ")
        body = raw
    else:
        found, body = _split_title(raw)
        title = found or source.stem.replace("_", " ")
    stripped = BUILD / (source.stem + ".body.md")
    stripped.write_text(body)

    subprocess.run(
        [
            "pandoc",
            str(stripped),
            "--standalone",
            "--from=gfm+yaml_metadata_block",
            "--to=html5",
            f"--css={css.name}",
            *(() if has_front_matter else ("--metadata", f"title={title}")),
            "--toc",
            "--toc-depth=3",
            "-o",
            str(html),
        ],
        check=True,
        cwd=ROOT,
    )
    stripped.unlink()

    html.write_text(_inline_svg(html.read_text(), source.parent))
    subprocess.run(
        ["node", str(js), str(html), str(pdf), f"Reimagina — {title}"],
        check=True,
        cwd=ROOT,
    )
    _pin_timestamps(pdf, _front_matter_date(raw))
    return pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render delivery docs to PDF.")
    parser.add_argument("names", nargs="*", help="Markdown files; default all.")
    args = parser.parse_args(argv)

    sources = (
        [DELIVERY / n for n in args.names]
        if args.names
        else sorted(DELIVERY.glob("*.md"))
    )
    missing = [s for s in sources if not s.is_file()]
    if missing:
        print(f"not found: {', '.join(str(m) for m in missing)}", file=sys.stderr)
        return 2
    if not sources:
        print(f"no Markdown in {DELIVERY.relative_to(ROOT)}", file=sys.stderr)
        return 2

    BUILD.mkdir(parents=True, exist_ok=True)
    css = BUILD / "print.css"
    css.write_text(PRINT_CSS)
    js = BUILD / "_render.js"
    js.write_text(RENDER_JS)

    for source in sources:
        pdf = render(source, css, js)
        print(
            f"  {source.name} -> {pdf.relative_to(ROOT)} "
            f"({pdf.stat().st_size // 1024} KB)"
        )
    js.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
