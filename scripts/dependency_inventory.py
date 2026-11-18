"""Every third-party dependency and its licence, read from what is installed.

Why this exists as a script and not a hand-written list: a licence inventory is
only useful if it matches the tree it claims to describe, and a static list
stops being true at the next dependency bump. Regenerate it instead:

    uv run python scripts/dependency_inventory.py > docs/DEPENDENCIES.md

⚠ It reports what is INSTALLED, not what is pinned. `uv.lock` records versions
but not licences, and `package-lock.json` records a `license` field that is
frequently absent — so the only honest source is the packages themselves. Run
it after `uv sync --locked` and `npm ci`, or the answer describes some other
tree. It fails loudly rather than emit a partial list.

Where a package's metadata declares no licence, this opens the package's own
LICENSE file and marks the entry so a reader can see the difference between
"the package says MIT" and "the package says nothing and a file looked like
MIT". Anything still unresolved is reported as UNRESOLVED rather than guessed:
a licence inventory that quietly invents an answer is worse than no inventory.
"""

from __future__ import annotations

import datetime
import email.parser
import json
from collections import Counter
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]

# Classifier strings and SPDX identifiers for the same licence. Normalising
# keeps the distribution table readable; the asterisk marks a classifier that
# names a family rather than a specific licence version.
_NORMALISE = {
    "MIT License": "MIT",
    "OSI Approved; MIT License": "MIT",
    "BSD License": "BSD-3-Clause*",
    "BSD": "BSD*",
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-3.0*",
    "BSD License; Apache Software License": "BSD-3-Clause OR Apache-2.0",
    "Apache Software License; MIT License": "Apache-2.0 OR MIT",
}

# Licences that are not plain permissive. Not a blocklist — a prompt to look.
CONCERNING = re.compile(r"LGPL|GPL|MPL|AGPL|EPL|CDDL|CC0|UNRESOLVED", re.I)


def _site_packages() -> Path:
    candidates = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    if not candidates:
        sys.exit(
            "no .venv found — run `uv sync --locked --extra dev` first; this "
            "script reads licences from installed metadata, not from uv.lock"
        )
    if len(candidates) > 1:
        # ⚠ Not `candidates[0]`. After a Python bump `.venv/lib` holds both the
        # old and the new interpreter directory, and string order puts the
        # *older* one first — `python3.12` sorts before `python3.13` — so the
        # inventory was generated from the stale tree and said so nowhere.
        #
        # Refusing rather than picking by `sys.version_info`: an ambiguous venv
        # is an operator problem with a one-line fix, and choosing silently
        # hides the staleness instead of reporting it. This document has to
        # match the tree it claims to describe.
        named = "\n  ".join(str(c.relative_to(ROOT)) for c in candidates)
        sys.exit(
            "more than one interpreter directory in .venv/lib, so which tree "
            "this inventory would describe is ambiguous:\n  "
            + named
            + "\nRebuild it: `rm -rf .venv && uv sync --locked --extra dev`"
        )
    return candidates[0]


def _licence_from_files(dist_info: Path) -> str | None:
    """First line of a bundled licence, when metadata declares nothing."""
    # `LICEN[CS]E` for both spellings: npm packages use `LICENCE` often enough
    # that `scripts/third_party_licences.py` already globs for it, and this
    # script reading only one of the two was the difference between a resolved
    # row and an UNRESOLVED one.
    for pattern in ("licenses/LICEN[CS]E*", "LICEN[CS]E*", "COPYING*"):
        for path in dist_info.glob(pattern):
            head = path.read_text(encoding="utf-8", errors="replace").strip()
            first = next((ln.strip() for ln in head.splitlines() if ln.strip()), "")
            if first:
                return f"{first[:60]} (from LICENSE file)"
    return None


def python_packages() -> list[tuple[str, str, str]]:
    rows = []
    for dist_info in sorted(_site_packages().glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.exists():
            continue
        parsed = email.parser.Parser().parsestr(
            metadata.read_text(encoding="utf-8", errors="replace")
        )
        licence = parsed.get("License-Expression") or ""
        if not licence:
            classifiers = [
                c.split("::")[-1].strip()
                for c in (parsed.get_all("Classifier") or [])
                if c.startswith("License ::")
            ]
            licence = "; ".join(classifiers) or (parsed.get("License") or "")
        licence = (licence or "").strip()
        # Some projects paste their whole licence into the field.
        if len(licence) > 60 or "\n" in licence:
            licence = ""
        licence = licence or _licence_from_files(dist_info) or "UNRESOLVED"
        # A dist-info without a Name is malformed; skipping it silently would
        # understate the inventory, so say so and keep going.
        name = parsed.get("Name")
        if not name:
            print(f"warning: {dist_info.name} declares no Name", file=sys.stderr)
            continue
        version = parsed.get("Version") or "unknown"
        rows.append((name, version, _NORMALISE.get(licence, licence)))
    return rows


def npm_packages() -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    lock = ROOT / "apps" / "web" / "package-lock.json"
    if not lock.exists():
        sys.exit("apps/web/package-lock.json missing")
    data = json.loads(lock.read_text())
    production: set[tuple[str, str, str]] = set()
    development: set[tuple[str, str, str]] = set()
    for key, entry in data.get("packages", {}).items():
        if not key:  # the root project itself
            continue
        name = entry.get("name") or key.split("node_modules/")[-1]
        licence = entry.get("license") or ""
        if not licence:
            # The lockfile often omits it even when the package ships one.
            #
            # ⚠ The *key*, not the name. A nested dependency's key is
            # `node_modules/a/node_modules/b` while its name is `b`, so joining
            # `node_modules` to the name looked for `node_modules/b` — which a
            # nested install does not have — and the row came out UNRESOLVED
            # with the licence file sitting at the key's own path. The key is
            # already the path relative to `apps/web`.
            #
            # `_licence_from_files` rather than a second copy of its glob-and-
            # read-first-line logic, which is what this block used to be.
            licence = _licence_from_files(ROOT / "apps" / "web" / key) or ""
        licence = licence or "UNRESOLVED"
        row = (name, entry.get("version", ""), licence)
        (development if entry.get("dev") else production).add(row)
    return sorted(production), sorted(development)


def _table(rows: list[tuple[str, str, str]]) -> list[str]:
    out = ["| Package | Version | Licence |", "|---|---|---|"]
    out += [
        f"| `{n}` | {v} | {lic} |"
        for n, v, lic in sorted(rows, key=lambda r: r[0].lower())
    ]
    return out


def _distribution(rows: list[tuple[str, str, str]]) -> list[str]:
    out = ["| Licence | Count |", "|---|---|"]
    out += [f"| {lic} | {n} |" for lic, n in Counter(r[2] for r in rows).most_common()]
    return out


def main() -> None:
    py = python_packages()
    prod, dev = npm_packages()
    flagged = [r for r in py + prod if CONCERNING.search(r[2])]

    lines = [
        "# Third-party dependency inventory",
        "",
        "<!-- Generated. Do not hand-edit:",
        "     uv run python scripts/dependency_inventory.py > docs/DEPENDENCIES.md -->",
        "",
        f"Generated {datetime.date.today().isoformat()} from installed package "
        "metadata, after `uv sync --locked --extra dev` and `npm ci`.",
        "",
        f"**{len(py)} Python distributions · {len(prod)} npm production packages · "
        f"{len(dev)} npm dev-only packages.**",
        "",
        "Licences come from each package's own metadata. Where metadata declares "
        "nothing, the package's LICENSE file is read and the entry says so. "
        "Anything still undetermined reads `UNRESOLVED` rather than a guess.",
        "",
        "## Not plainly permissive — look before shipping",
        "",
        "Copyleft, public-domain dedications, and anything unresolved. This is a "
        "prompt to check, not a blocklist; a dev-only dependency is not shipped.",
        "",
    ]
    lines += _table(flagged) if flagged else ["None."]
    lines += ["", "## Python — licence distribution", ""]
    lines += _distribution(py)
    lines += ["", "## npm production — licence distribution", ""]
    lines += _distribution(prod)
    lines += ["", "## Python — full list", ""]
    lines += _table(py)
    lines += ["", "## npm production — full list", ""]
    lines += _table(prod)
    lines += ["", f"## npm dev-only — {len(dev)} packages, not shipped", ""]
    lines += _distribution(dev)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
