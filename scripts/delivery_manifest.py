"""Generate the delivery manifest Article 2.2 requires.

The agreement identifies a delivery by **date, version, commit or tag, and
SHA-256 hash**. Three of those were partly present before this script — each
document declared a date and carried a revision table, and the commit existed
— and the fourth did not exist at all.

Two files come out of one pass, because they serve different readers:

``SHA256SUMS``
    Plain, and exactly what ``sha256sum -c`` accepts. This is the one the
    recipient runs. A manifest whose hashes can only be compared by eye is
    decoration; this is what makes it checkable.

``MANIFEST.md``
    The same hashes with the date, the version, the commit, the tag, and
    which Schedule A Work each artefact satisfies. This is the one that goes
    in the delivery email.

⚠ **The manifest cannot contain its own hash**, and neither file can cover
itself. ``SHA256SUMS`` covers the artefacts; both files are in turn covered
by the commit the tag points at. That is the ordinary resolution of the
circularity, and it is written into the generated document rather than left
for a reviewer to notice.

**What is not hashed:** the source tree. The tag identifies it, and a tree
hash stored in a file inside that tree is a circularity with no reader. What
makes the *documents* verifiable is something better than a hash anyway —
they are byte-reproducible, so a recipient can rebuild them from the tagged
commit and compare rather than trust. See ``render_delivery_pdfs._pin_timestamps``.

Usage::

    python scripts/delivery_manifest.py            # write both files
    python scripts/delivery_manifest.py --check    # fail if either is stale
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "docs" / "delivery"
SUMS = DELIVERY / "SHA256SUMS"
MANIFEST = DELIVERY / "MANIFEST.md"

#: Each delivered artefact and the Schedule A Work it satisfies. Explicit
#: rather than globbed: a glob would silently ship a file nobody decided to
#: deliver, and silently drop one whose name changed.
ARTEFACTS: tuple[tuple[str, str], ...] = (
    ("docs/delivery/pdf/01_architecture_map.pdf", "Work 1 — architecture map"),
    ("docs/delivery/01_architecture_map.md", "Work 1 — source"),
    (
        "docs/delivery/pdf/03_database_and_api_reference.pdf",
        "Work 3 — database and API reference",
    ),
    ("docs/delivery/03_database_and_api_reference.md", "Work 3 — source"),
    (
        "docs/delivery/pdf/04_technical_documentation.pdf",
        "Work 4 — technical documentation",
    ),
    ("docs/delivery/04_technical_documentation.md", "Work 4 — source"),
    ("docs/delivery/pdf/05_capability_proof.pdf", "Annex — capability proof"),
    ("docs/delivery/05_capability_proof.md", "Annex — source"),
    ("docs/THIRD_PARTY_LICENSES.md", "Article 4.1 — dependency inventory"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    """A git value, or an empty string outside a repository.

    Empty rather than raising: the manifest is still worth generating from an
    exported tree, and a missing commit is visible in the output as a blank
    where a hash belongs.
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _delivery_date() -> str:
    """The date the documents themselves declare.

    Taken from the documents rather than from the clock, so regenerating the
    manifest a week later does not silently restate the delivery date — and
    so a disagreement between the four documents surfaces here instead of in
    the recipient's inbox.
    """
    declared = set()
    for relative, _ in ARTEFACTS:
        path = ROOT / relative
        if path.suffix != ".md" or not path.is_file():
            continue
        head = path.read_text()[:600]
        for line in head.splitlines():
            if line.startswith("date:"):
                declared.add(line.split(":", 1)[1].strip().strip('"').strip("'"))
                break
    if len(declared) > 1:
        raise SystemExit(
            "the delivery documents declare different dates, so the manifest "
            f"cannot state one: {sorted(declared)}"
        )
    return declared.pop() if declared else "(no date declared)"


def _rows() -> list[tuple[str, str, int, str]]:
    rows = []
    missing = []
    for relative, work in ARTEFACTS:
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        rows.append((relative, _sha256(path), path.stat().st_size, work))
    if missing:
        raise SystemExit(
            "cannot build a manifest with artefacts missing — run "
            "`python scripts/render_delivery_pdfs.py` first:\n  " + "\n  ".join(missing)
        )
    return rows


def _sums(rows: list[tuple[str, str, int, str]]) -> str:
    """`sha256sum -c` format: hash, two spaces, path. Nothing else."""
    return "".join(f"{digest}  {relative}\n" for relative, digest, _, _ in rows)


def _manifest(rows: list[tuple[str, str, int, str]], date: str) -> str:
    commit = _git("rev-parse", "HEAD")
    tag = _git("describe", "--tags", "--exact-match") or "(untagged)"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git("status", "--porcelain"))

    lines = [
        "# Delivery manifest",
        "",
        "Issued under Article 2.2 of the agreement, which identifies a delivery",
        "by its date, version, commit or tag, and SHA-256 hash.",
        "",
        "| | |",
        "|---|---|",
        f"| **Date** | {date} |",
        f"| **Tag** | `{tag}` |",
        f"| **Generated from commit** | `{commit or '(not a git repository)'}` |",
        f"| **Branch** | `{branch or '—'}` |",
        f"| **Working tree at generation** | {'MODIFIED — not a clean delivery' if dirty else 'clean'} |",
        "",
        "⚠ The tag is the authoritative identifier. The commit above is the one",
        "this file was generated *from*, which is necessarily the commit before",
        "the one that adds it — a file cannot record the hash of the commit that",
        "contains it. Resolve the two with `git rev-list -n 1 <tag>`.",
        "",
        "## Artefacts",
        "",
        "| Schedule A | File | Bytes | SHA-256 |",
        "|---|---|---:|---|",
    ]
    for relative, digest, size, work in rows:
        lines.append(f"| {work} | `{relative}` | {size:,} | `{digest}` |")

    lines += [
        "",
        "## Verifying this",
        "",
        "From the root of a checkout of the tag above:",
        "",
        "```bash",
        "sha256sum -c docs/delivery/SHA256SUMS",
        "```",
        "",
        "Every line must read `OK`. `SHA256SUMS` carries the same hashes in the",
        "format that command expects, so nothing has to be compared by eye.",
        "",
        "### Rebuilding the documents instead of trusting them",
        "",
        "The PDFs are byte-reproducible. Rendering them from the same sources",
        "produces the same bytes, because the only non-determinism in the",
        "pipeline — the two timestamps the PDF engine embeds — is pinned to the",
        "date each document declares.",
        "",
        "```bash",
        "python scripts/render_delivery_pdfs.py",
        "sha256sum -c docs/delivery/SHA256SUMS",
        "```",
        "",
        "That is a stronger check than the first one: it needs no trust in the",
        "delivered files at all, only in the sources the tag identifies.",
        "",
        "### What a hash does and does not establish",
        "",
        "A SHA-256 proves these bytes are the bytes that were delivered — it",
        "detects corruption in transit and accidental substitution. It is not a",
        "signature and does not establish authorship, because that would need a",
        "key the recipient has independent reason to trust, and no such key",
        "exists in this engagement.",
        "",
        "⚠ Neither this file nor `SHA256SUMS` can cover itself. The artefacts",
        "are covered by `SHA256SUMS`; both files are covered by the commit the",
        "tag points at.",
        "",
        "---",
        "",
        "Generated by `scripts/delivery_manifest.py`. Do not edit by hand — a",
        "contract test regenerates it and fails if the result differs.",
        "",
    ]
    return "\n".join(lines)


#: Rows whose value comes from git rather than from the artefacts.
#:
#: ⚠ These are why `--check` cannot compare the manifest byte for byte.
#: Committing the manifest moves HEAD, so the commit it records is stale the
#: instant it is committed — a check that compared the whole file could never
#: pass on a committed manifest, which would make it useless rather than
#: strict. What `--check` is for is "do the recorded hashes still describe
#: these files"; the git rows are provenance for a reader, not a claim that
#: can rot into a wrong hash.
_GIT_ROWS = (
    "| **Tag** |",
    "| **Generated from commit** |",
    "| **Branch** |",
    "| **Working tree at generation** |",
)


def _without_git_rows(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.startswith(_GIT_ROWS)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if either generated file is out of date.",
    )
    args = parser.parse_args(argv)

    rows = _rows()
    sums = _sums(rows)
    manifest = _manifest(rows, _delivery_date())

    if args.check:
        # SHA256SUMS compares exactly — it carries nothing but hashes and
        # paths. The manifest compares with its git rows removed; see
        # `_GIT_ROWS` for why a byte-for-byte check there could never pass.
        stale = [
            path.relative_to(ROOT)
            for path, want, normalise in (
                (SUMS, sums, str),
                (MANIFEST, manifest, _without_git_rows),
            )
            if not path.is_file() or normalise(path.read_text()) != normalise(want)
        ]
        if stale:
            print(
                "stale: " + ", ".join(str(s) for s in stale) + "\n"
                "Run `python scripts/delivery_manifest.py` after rendering.",
                file=sys.stderr,
            )
            return 1
        print(f"{SUMS.relative_to(ROOT)} and {MANIFEST.relative_to(ROOT)} are current.")
        return 0

    SUMS.write_text(sums)
    MANIFEST.write_text(manifest)
    print(
        f"wrote {SUMS.relative_to(ROOT)} and {MANIFEST.relative_to(ROOT)} — "
        f"{len(rows)} artefacts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
