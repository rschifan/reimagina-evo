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

The delivery package (``docs/delivery/``) is kept out of the repository, so
neither file is committed: both are generated from the tagged commit and
travel, with the PDFs, as assets of the GitHub release. Neither file covers
itself; ``SHA256SUMS`` covers the artefacts.

**The source code (Schedule A Work 2)** is hashed as the uncompressed
``git archive`` tar of the tagged commit. Uncompressed because a ``.tar.gz``
depends on the gzip implementation; the tar's bytes depend only on the
commit (its entries carry the commit time).

Generate on a clean ``main`` checked out at the tag: the recorded commit must
be the one the tag names.

Usage::

    python scripts/delivery_manifest.py --tag v1.0.0   # write both files
    python scripts/delivery_manifest.py --check        # fail if either is stale
    python scripts/delivery_manifest.py --print-commit # the source commit recorded
"""

from __future__ import annotations

import argparse
import hashlib
import re
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
    (
        "docs/delivery/pdf/03_database_and_api_reference.pdf",
        "Work 3 — database and API reference",
    ),
    (
        "docs/delivery/pdf/04_technical_documentation.pdf",
        "Work 4 — technical documentation",
    ),
    ("docs/THIRD_PARTY_LICENSES.md", "Article 4.1 — dependency inventory"),
)

#: The sources the delivery date is read from; local, not delivered.
SOURCES = tuple(
    f"docs/delivery/{name}.md"
    for name in (
        "01_architecture_map",
        "03_database_and_api_reference",
        "04_technical_documentation",
    )
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


def _version(explicit: str | None) -> str:
    """The delivery version: `--version`, else `pyproject.toml`."""
    if explicit:
        return explicit.removeprefix("v")
    text = (ROOT / "pyproject.toml").read_text()
    found = re.search(r'^version = "([^"]+)"', text, re.M)
    return found.group(1) if found else "(unknown)"


def _source_archive(commit: str, version: str) -> tuple[str, int]:
    """SHA-256 and size of the uncompressed `git archive` of `commit`."""
    if not commit:
        return "(not a git repository)", 0
    tar = subprocess.run(
        ["git", "archive", "--format=tar", f"--prefix=r-evo-v{version}/", commit],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(tar).hexdigest(), len(tar)


def _delivery_date() -> str:
    """The date the documents themselves declare.

    Taken from the documents rather than from the clock, so regenerating the
    manifest a week later does not silently restate the delivery date — and
    so a disagreement between the four documents surfaces here instead of in
    the recipient's inbox.
    """
    declared = set()
    for relative in SOURCES:
        path = ROOT / relative
        if not path.is_file():
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


def _manifest(
    rows: list[tuple[str, str, int, str]],
    date: str,
    tag: str | None = None,
    version: str = "(unknown)",
) -> str:
    commit = _git("rev-parse", "HEAD")
    src_sha, src_size = _source_archive(commit, version)
    # Discovered when HEAD carries a tag, stated with `--tag` otherwise.
    tag = tag or _git("describe", "--tags", "--exact-match") or "(untagged)"
    # What a recipient checks out. Both verification procedures start from a
    # checkout, and "(untagged)" names nothing to check out, so an untagged
    # manifest has to say what stands in for the tag. It is a git row, like
    # the tag itself, so the prose below is the same either way and `--check`
    # keeps comparing it.
    if tag == "(untagged)":
        checkout = (
            "⚠ untagged, so not yet an issued delivery: tag "
            f"`{(commit or '?')[:12]}` before issuing."
        )
    else:
        checkout = f"`git checkout {tag}`"

    lines = [
        "# Delivery manifest",
        "",
        "Delivered under Article 2.1, identified under Article 2.2 of the",
        "agreement, which identifies a delivery by its date, version, commit or",
        "tag, and SHA-256 hash.",
        "",
        "| | |",
        "|---|---|",
        f"| **Date** | {date} |",
        f"| **Version** | {version} |",
        f"| **Tag** | `{tag}` |",
        f"| **Generated from commit** | `{commit or '(not a git repository)'}` |",
        f"| **Check out** | {checkout} |",
        "",
        "The tag is the authoritative identifier and names the commit above;",
        "resolve it with `git rev-list -n 1 <tag>`.",
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
        "## Source code (Schedule A Work 2)",
        "",
        "| Schedule A | Source commit | Bytes | SHA-256 |",
        "|---|---|---:|---|",
        "| Work 2 — source code, `git archive` of the source commit | "
        f"`{commit or '(not a git repository)'}` | {src_size:,} | `{src_sha}` |",
        "",
        "The hash is of the tagged commit's *uncompressed* `git archive` tar,",
        "because a `.tar.gz` also depends on the gzip implementation. To",
        "recompute it from any clone:",
        "",
        "```bash",
        f"git archive --format=tar --prefix=r-evo-v{version}/ "
        f"{commit or '<commit>'} | sha256sum",
        "```",
        "",
        f"The release also carries `r-evo-v{version}.tar.gz`, the same",
        "archive compressed with `gzip -n`; its own hash is in the release asset",
        "`SHA256SUMS-assets`, which lists every file of the release.",
        "",
        "## Verifying this",
        "",
        "The PDFs are **not in the repository**. They travel as assets of the",
        f"GitHub release of tag `{tag}`, together with `MANIFEST.md`,",
        "`SHA256SUMS` and the source archive. Check out the tag, put the PDFs",
        "in `docs/delivery/pdf/` and `SHA256SUMS` at the repository root, then:",
        "",
        "```bash",
        "sha256sum -c SHA256SUMS",
        "```",
        "",
        "Every line must read `OK`. `SHA256SUMS` carries the same hashes as the",
        "table above, in the format that command expects, so nothing has to be",
        "compared by eye.",
        "",
        "### What a hash does and does not establish",
        "",
        "A SHA-256 proves these bytes are the bytes that were delivered — it",
        "detects corruption in transit and accidental substitution. It is not a",
        "signature and does not establish authorship, because that would need a",
        "key the recipient has independent reason to trust, and no such key",
        "exists in this engagement.",
        "",
        "Neither this file nor `SHA256SUMS` can cover itself; the artefacts are",
        "covered by `SHA256SUMS`.",
        "",
        "---",
        "",
        "Generated by `scripts/delivery_manifest.py`. Do not edit by hand.",
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
    "| **Check out** |",
    "| Work 2 ",
    "git archive ",
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
    parser.add_argument(
        "--tag",
        default=None,
        metavar="NAME",
        help=("The tag this delivery carries; default: the tag on HEAD."),
    )
    parser.add_argument("--version", default=None, help="Default: pyproject.toml.")
    parser.add_argument(
        "--print-commit", action="store_true", help="Print the source commit and exit."
    )
    args = parser.parse_args(argv)

    if args.print_commit:
        print(_git("rev-parse", "HEAD"))
        return 0
    rows = _rows()
    sums = _sums(rows)
    manifest = _manifest(rows, _delivery_date(), args.tag, _version(args.version))

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
