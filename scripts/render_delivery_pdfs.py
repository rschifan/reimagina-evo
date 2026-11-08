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
#title-block-header { margin: 0 0 1.6em; padding-bottom: .7em;
                      border-bottom: 2px solid #16181d; }
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


def render(source: Path, css: Path, js: Path) -> Path:
    html = BUILD / (source.stem + ".html")
    pdf = BUILD / (source.stem + ".pdf")
    found, body = _split_title(source.read_text())
    title = found or source.stem.replace("_", " ")
    stripped = BUILD / (source.stem + ".body.md")
    stripped.write_text(body)

    subprocess.run(
        [
            "pandoc",
            str(stripped),
            "--standalone",
            "--from=gfm",
            "--to=html5",
            f"--css={css.name}",
            "--metadata",
            f"title={title}",
            "--toc",
            "--toc-depth=3",
            "-o",
            str(html),
        ],
        check=True,
        cwd=ROOT,
    )
    stripped.unlink()
    subprocess.run(
        ["node", str(js), str(html), str(pdf), f"Reimagina — {title}"],
        check=True,
        cwd=ROOT,
    )
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
