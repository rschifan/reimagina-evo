"""Render the delivery diagrams from Mermaid sources to committed SVG.

The diagrams are authored as Mermaid (``docs/delivery/diagrams/*.mmd``) and
committed as SVG beside their sources. That split is deliberate: a delivered
document must not depend on a toolchain the recipient has to reconstruct, so
the PDF build needs only pandoc and Chromium, while Mermaid is an authoring
tool that runs when a diagram actually changes.

Committing the ``.mmd`` alongside keeps the diagrams editable rather than
frozen — a diagram nobody can change is one that silently stops matching the
system.

Mermaid is not a dependency of this project and must not become one: adding it
to ``apps/web`` would change ``node_modules``, and the third-party licence
inventory is derived from exactly that tree, so the deliverable would start
contradicting itself. Install it anywhere outside the repository::

    mkdir -p /tmp/mermaid && cd /tmp/mermaid && npm init -y && npm install mermaid

then point this script at it with ``--mermaid`` or ``$MERMAID_DIST``.

Usage::

    python scripts/build_diagrams.py --mermaid /tmp/mermaid/node_modules/mermaid/dist
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "docs" / "delivery" / "diagrams"

# Rendered in a headless page, then the <svg> element is lifted out whole.
# Mermaid's own CLI bundles a second browser; this reuses the one already here.
RENDER_JS = """
const {chromium} = require('playwright');
const fs = require('fs');
const [page_url, specPath, outDir] = process.argv.slice(2);
const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: 1400, height: 1000}});
  await page.goto(page_url, {waitUntil: 'load'});
  for (const {name, source} of spec) {
    const svg = await page.evaluate(async ({name, source}) => {
      const {svg} = await window.mermaid.render('d_' + name, source);
      return svg;
    }, {name, source});
    fs.writeFileSync(outDir + '/' + name + '.svg', svg);
    console.log('  ' + name + '.svg');
  }
  await browser.close();
})().catch((err) => { console.error(err); process.exit(1); });
"""

HOST_HTML = """<!doctype html><html><head><meta charset="utf-8">
<script src="{mermaid}/mermaid.min.js"></script>
<script>
  mermaid.initialize({{
    startOnLoad: false, theme: 'neutral', securityLevel: 'strict',
    flowchart: {{ htmlLabels: false, curve: 'basis', nodeSpacing: 40,
                  rankSpacing: 60, wrappingWidth: 260, padding: 14 }},
    fontFamily: 'DejaVu Sans, Helvetica Neue, Arial, sans-serif',
    themeVariables: {{ fontSize: '28px' }},
  }});
  window.mermaid = mermaid;
</script></head><body></body></html>
"""


def _give_intrinsic_size(svg: Path) -> None:
    """Replace Mermaid's ``width="100%"`` with the size from the viewBox.

    Mermaid emits a root element sized for a flexible web container: a
    percentage width, no height, and a ``max-width`` style. That is fine in a
    page it can measure itself against, and useless in an ``<img>``, where it
    leaves the file with no intrinsic size at all — the browser renders a
    broken image rather than a diagram, and the PDF comes out looking
    perfectly fine with nothing on the page. Giving the root a concrete pixel
    size makes each file a standalone image.
    """
    text = svg.read_text()
    box = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"', text)
    if not box:
        return
    width, height = round(float(box.group(1))), round(float(box.group(2)))
    text = re.sub(
        r'\swidth="100%"', f' width="{width}" height="{height}"', text, count=1
    )
    text = re.sub(r'\sstyle="max-width:[^"]*"', "", text, count=1)
    svg.write_text(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Mermaid sources to SVG.")
    parser.add_argument(
        "--mermaid",
        default=os.environ.get("MERMAID_DIST", ""),
        help="Directory holding mermaid.min.js. Defaults to $MERMAID_DIST.",
    )
    args = parser.parse_args(argv)

    sources = sorted(DIAGRAMS.glob("*.mmd"))
    if not sources:
        print(f"no .mmd sources in {DIAGRAMS.relative_to(ROOT)}", file=sys.stderr)
        return 2

    dist = Path(args.mermaid)
    if not (dist / "mermaid.min.js").is_file():
        print(
            "mermaid.min.js not found. Install Mermaid outside the repository "
            "and pass --mermaid <dir> (see this script's docstring).",
            file=sys.stderr,
        )
        return 2

    spec = [{"name": s.stem, "source": s.read_text()} for s in sources]
    with tempfile.TemporaryDirectory() as tmp:
        host = Path(tmp) / "host.html"
        host.write_text(HOST_HTML.format(mermaid=dist.as_uri()))
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec))
        js = Path(tmp) / "render.js"
        js.write_text(RENDER_JS)
        subprocess.run(
            ["node", str(js), host.as_uri(), str(spec_path), str(DIAGRAMS)],
            check=True,
            cwd=ROOT,
        )

    for source in sources:
        _give_intrinsic_size(DIAGRAMS / f"{source.stem}.svg")

    print(f"{len(sources)} diagram(s) -> {DIAGRAMS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
