"""Generate the third-party dependency and licence inventory.

A delivery of this platform has to be accompanied by a complete list of the
third-party and open-source code it incorporates, with each item's licence
terms, so that whoever receives it can see what they are taking on. This script
produces that list.

It is a script rather than a hand-written document on purpose, and the reason
is in this repository's own history: ``overridable_attributes()`` says a
hand-maintained list "is how ``poi_version`` rotted — it named an input nothing
produced and nothing validated, for six migrations". A licence inventory rots
the same way and the rot is less visible, because nothing fails when it goes
stale. Regenerate it; do not edit the output.

What it reads:

- **Python** — the distributions actually installed in the environment, via
  ``importlib.metadata``, not the lockfile. The lockfile says what should be
  resolved; the installed set is what a reader can actually inspect, and the
  licence metadata lives there.
- **npm** — every ``package.json`` under ``apps/web/node_modules``, plus
  ``npm ls --omit=dev`` to mark the subset that reaches the production bundle.
  That distinction matters: a build-time formatter and a library shipped to
  every browser are not the same disclosure.

Where a package declares no licence, the script looks for a ``LICENSE`` file
beside it and reports what it finds, labelled as coming from the file rather
than the manifest. It never guesses: an unresolved package is reported as
undeclared, because "probably MIT" is not a licence statement.

Usage::

    python scripts/third_party_licences.py            # writes the document
    python scripts/third_party_licences.py --check    # exit 1 if out of date
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_MODULES = ROOT / "apps" / "web" / "node_modules"
OUTPUT = ROOT / "docs" / "THIRD_PARTY_LICENSES.md"

# This project's own distribution is not a third party to itself.
OWN_PACKAGES = {"reimagina"}

# Findings that cannot be read off a manifest, recorded here so regenerating
# the document does not discard the research. Keep each one to what was
# actually checked — an inventory that launders an assumption into a statement
# is worse than one that admits a gap.
MANUAL_NOTES: dict[str, str] = {
    "@mapbox/jsonlint-lines-primitives": (
        "`@mapbox/jsonlint-lines-primitives` 2.0.2 declares no licence: no "
        "`license` field, no LICENSE file and no copyright header in the "
        "published package. Both of its source repositories state MIT — "
        "github.com/mapbox/jsonlint carries a LICENSE file and is labelled "
        "MIT, and the upstream github.com/zaach/jsonlint README states "
        "'MIT License, Copyright (C) 2012 Zachary Carter'. That was **not** "
        "confirmed against this published version: the npm registry page, the "
        "only source tying a licence to 2.0.2, returned HTTP 403. It reaches "
        "the browser as a transitive dependency of `maplibre-gl`."
    ),
}

# Order matters: LGPL must be recognised before GPL, or every LGPL package is
# reported as strong copyleft and the one category that would stop a delivery
# stops meaning anything.
WEAK_COPYLEFT = re.compile(
    r"\bLGPL|Lesser General|\bMPL\b|MPL-|Mozilla Public|\bEPL-|Eclipse Public"
    r"|\bCDDL|Common Development",
    re.I,
)
STRONG_COPYLEFT = re.compile(
    r"\bAGPL|\bGPL-|\bGPLv|GNU General Public|\bSSPL|Open Software License"
    r"|\bOSL-|\bCPAL|\bEUPL",
    re.I,
)
PERMISSIVE = re.compile(
    r"\bMIT\b|\bBSD\b|BSD-[0-9]|Apache|\bISC\b|\b0BSD\b|Unlicense|\bCC0\b"
    r"|Python-2|\bPSF\b|\bZlib\b|Blue ?Oak|\bWTFPL|Public Domain|CC-BY(?!-SA)",
    re.I,
)

STRONG = "strong-copyleft"
WEAK = "weak-copyleft"
PERM = "permissive"
UNDECLARED = "undeclared"


def classify(text: str) -> str:
    """Bucket a licence string. Weak copyleft is tested first — see above."""
    if WEAK_COPYLEFT.search(text):
        return WEAK
    if STRONG_COPYLEFT.search(text):
        return STRONG
    if PERMISSIVE.search(text):
        return PERM
    return UNDECLARED


@dataclass(frozen=True)
class Entry:
    ecosystem: str
    name: str
    version: str
    licence: str
    source: str  # where the licence text came from
    production: bool

    @property
    def bucket(self) -> str:
        return classify(self.licence)

    @property
    def distribution(self) -> str:
        """Where this code actually runs.

        Not one column for both ecosystems: a Python distribution never
        reaches a browser, so reporting one as shipping in the bundle would be
        a false statement in a document whose whole purpose is accuracy.
        """
        if self.ecosystem == "python":
            return "server-side"
        return "browser bundle" if self.production else "build/test only"


def _licence_from_file(directory: Path) -> tuple[str, str]:
    """Read a licence out of a LICENSE file when the manifest declares none."""
    for pattern in ("LICENSE*", "LICENCE*", "COPYING*"):
        for path in sorted(directory.glob(pattern)):
            if not path.is_file():
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
            for name in ("MIT", "Apache", "BSD", "ISC", "MPL", "LGPL", "GPL"):
                if name.lower() in head.lower():
                    return f"{name} (per {path.name}; not declared in manifest)", "file"
            return f"see {path.name}", "file"
    return "", ""


def python_entries() -> list[Entry]:
    entries: list[Entry] = []
    for dist in distributions():
        meta = dist.metadata
        name = meta["Name"] or dist.name
        if not name or name in OWN_PACKAGES:
            continue
        classifiers = [
            c
            for c in (meta.get_all("Classifier") or [])
            if str(c).startswith("License")
        ]
        declared = ""
        source = "metadata"
        if "License-Expression" in meta and meta["License-Expression"]:
            declared = str(meta["License-Expression"]).strip()
        elif classifiers:
            declared = "; ".join(str(c).split("::")[-1].strip() for c in classifiers)
            source = "classifier"
        elif meta["License"]:
            raw = str(meta["License"]).strip()
            declared = raw if len(raw) < 80 else raw[:77] + "..."
        entries.append(
            Entry(
                "python",
                str(name),
                str(meta["Version"] or dist.version or "?"),
                declared,
                source if declared else "",
                production=True,  # no runtime/dev split is recorded at install time
            )
        )
    return entries


def _npm_production_names() -> set[str]:
    try:
        proc = subprocess.run(
            ["npm", "ls", "--omit=dev", "--all", "--json"],
            cwd=ROOT / "apps" / "web",
            capture_output=True,
            text=True,
            check=False,
        )
        tree = json.loads(proc.stdout or "{}")
    except (OSError, json.JSONDecodeError):
        return set()

    names: set[str] = set()

    def walk(node: dict) -> None:
        for key, value in (node.get("dependencies") or {}).items():
            names.add(key)
            if isinstance(value, dict):
                walk(value)

    walk(tree)
    return names


def _npm_package_dirs(base: Path):
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if entry.name.startswith("@"):
            yield from (c for c in sorted(entry.iterdir()) if c.is_dir())
        else:
            yield entry


def npm_entries() -> list[Entry]:
    if not NODE_MODULES.is_dir():
        return []
    production = _npm_production_names()
    entries: list[Entry] = []
    for directory in _npm_package_dirs(NODE_MODULES):
        manifest = directory / "package.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        raw = data.get("license") or data.get("licenses") or ""
        if isinstance(raw, list):
            raw = " OR ".join(
                str(x.get("type", x)) if isinstance(x, dict) else str(x) for x in raw
            )
        elif isinstance(raw, dict):
            raw = str(raw.get("type", ""))
        licence, source = str(raw).strip(), "manifest"
        if not licence:
            licence, source = _licence_from_file(directory)
        name = str(data.get("name") or directory.name)
        entries.append(
            Entry(
                "npm",
                name,
                str(data.get("version") or "?"),
                licence,
                source,
                production=name in production,
            )
        )
    return entries


def _table(entries: list[Entry]) -> list[str]:
    lines = ["| Package | Version | Licence | Distribution |", "|---|---|---|---|"]
    for e in sorted(entries, key=lambda x: x.name.lower()):
        licence = e.licence or "**undeclared**"
        lines.append(f"| `{e.name}` | {e.version} | {licence} | {e.distribution} |")
    return lines


def render(entries: list[Entry]) -> str:
    python = [e for e in entries if e.ecosystem == "python"]
    npm = [e for e in entries if e.ecosystem == "npm"]
    flagged = [e for e in entries if e.bucket in (STRONG, WEAK, UNDECLARED)]

    out: list[str] = [
        "# Third-party dependencies and licences",
        "",
        "Complete inventory of the third-party and open-source code this platform",
        "incorporates, with the licence each one declares.",
        "",
        "> **Generated — do not edit.** Regenerate with",
        "> `python scripts/third_party_licences.py`, which reads the installed",
        "> Python distributions and every `package.json` under",
        "> `apps/web/node_modules`. Regenerate before any delivery, so the list",
        "> describes the code actually being delivered rather than the code that",
        "> was installed when someone last remembered.",
        "",
        "`Distribution` separates code that reaches a browser from code used",
        "only to build or test, and from server-side Python. A build-time",
        "formatter and a library served to every user are not the same",
        "disclosure.",
        "",
        "## Summary",
        "",
        "| | Count |",
        "|---|---|",
        f"| Python distributions | {len(python)} |",
        f"| npm packages | {len(npm)} |",
        f"| of which ship in the production bundle | {len([e for e in npm if e.production])} |",
        f"| **Strong copyleft (GPL / AGPL / SSPL)** | **{len([e for e in entries if e.bucket == STRONG])}** |",
        f"| Weak copyleft (LGPL / MPL / EPL / CDDL) | {len([e for e in entries if e.bucket == WEAK])} |",
        f"| Undeclared | {len([e for e in entries if e.bucket == UNDECLARED])} |",
        f"| Permissive | {len([e for e in entries if e.bucket == PERM])} |",
        "",
        "## Needing attention",
        "",
        "Everything not in this section declares a permissive licence (MIT, BSD,",
        "Apache-2.0, ISC and similar) that imposes no restriction beyond",
        "attribution.",
        "",
    ]
    if flagged:
        out += [
            "| Package | Version | Licence | Category | Distribution |",
            "|---|---|---|---|---|",
        ]
        for e in sorted(flagged, key=lambda x: (x.bucket, x.name.lower())):
            out.append(
                f"| `{e.name}` | {e.version} | {e.licence or '**undeclared**'} "
                f"| {e.bucket} | {e.distribution} |"
            )
        notes = [n for n in (MANUAL_NOTES.get(e.name) for e in flagged) if n]
        if notes:
            out += ["", "### Findings on specific packages", ""]
            out += [f"- {note}" for note in sorted(set(notes))]
    else:
        out.append("None.")
    out += ["", "## Python", ""] + _table(python)
    out += ["", "## npm", ""] + _table(npm)
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the committed document is out of date instead of writing it.",
    )
    args = parser.parse_args(argv)

    entries = python_entries() + npm_entries()
    document = render(entries)

    if args.check:
        current = OUTPUT.read_text() if OUTPUT.is_file() else ""
        if current != document:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; regenerate it.")
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(document)
    print(f"wrote {OUTPUT.relative_to(ROOT)} — {len(entries)} dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
