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

**The source code (Schedule A Work 2)** is hashed too, but not as "the tag":
the tag's commit contains this very file, so a hash of it cannot be written
inside it. The manifest hashes the *source commit* — the commit it was
generated from, which is the tag's parent — as an uncompressed
``git archive`` tar. Uncompressed because a ``.tar.gz`` depends on the gzip
implementation; the tar's bytes depend only on the commit (its entries carry
the commit time). The tag's commit differs from the source commit by exactly
``docs/delivery/MANIFEST.md`` and ``docs/delivery/SHA256SUMS``.

Generate on a clean ``main``, never on a feature branch: the recorded commit
must be one the recipient can reach from the tag.

The *documents* are verifiable by something better than a hash as well: they
are byte-reproducible, so a recipient can rebuild them and compare rather than
trust. See ``render_delivery_pdfs._pin_timestamps``.

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
        ["git", "archive", "--format=tar", f"--prefix=reimagina-v{version}/", commit],
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


def _manifest(
    rows: list[tuple[str, str, int, str]],
    date: str,
    tag: str | None = None,
    version: str = "(unknown)",
) -> str:
    commit = _git("rev-parse", "HEAD")
    src_sha, src_size = _source_archive(commit, version)
    # Discovered when the tag already exists, stated when it does not.
    # The tag has to name the commit whose tree contains this file, so it
    # cannot exist when the file is generated — the ordering is: state the
    # name here, commit, then tag that commit. Without `--tag` the manifest
    # would permanently read "(untagged)", which is the one field a
    # recipient is most likely to need.
    tag = tag or _git("describe", "--tags", "--exact-match") or "(untagged)"
    # What a recipient checks out. Both verification procedures start from a
    # checkout, and "(untagged)" names nothing to check out, so an untagged
    # manifest has to say what stands in for the tag. It is a git row, like
    # the tag itself, so the prose below is the same either way and `--check`
    # keeps comparing it.
    if tag == "(untagged)":
        checkout = (
            "⚠ untagged, so not yet an issued delivery: the commit that adds "
            f"this file, the first after `{(commit or '?')[:12]}` to touch "
            "`docs/delivery/MANIFEST.md`. Tag that commit before issuing."
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
        "⚠ The tag is the authoritative identifier, and the **Check out** row",
        "names what to check out for the procedures below. The commit above is",
        "the one this file was generated *from*, which is necessarily the commit",
        "before the one that adds it — a file cannot record the hash of the",
        "commit that contains it. Resolve a tag to its commit with",
        "`git rev-list -n 1 <tag>`.",
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
        "The hash is of the commit named above — the one this manifest was",
        "generated from, which is the tag's parent — not of the tag itself: the",
        "tag's commit contains this file, and a file cannot hold the hash of",
        "something that contains it. The tag's commit differs from the source",
        "commit by exactly `docs/delivery/MANIFEST.md` and",
        "`docs/delivery/SHA256SUMS`. The archive is the *uncompressed* tar,",
        "because a `.tar.gz` also depends on the gzip implementation. To",
        "recompute it from any clone:",
        "",
        "```bash",
        f"git archive --format=tar --prefix=reimagina-v{version}/ "
        f"{commit or '<commit>'} | sha256sum",
        "```",
        "",
        f"The release also carries `reimagina-v{version}.tar.gz`, the same",
        "archive compressed with `gzip -n`; its own hash is in the release asset",
        "`SHA256SUMS-assets`, which lists every file of the release.",
        "",
        "## Verifying this",
        "",
        "The four PDFs are **not in the repository**: they are generated, and",
        "`.gitignore` excludes `docs/delivery/pdf/`. They travel as assets of",
        f"the GitHub release of tag `{tag}`, together with `MANIFEST.md`,",
        "`SHA256SUMS` and the source archive. A bare checkout therefore reports",
        "four `FAILED open or read` lines from `sha256sum -c`, which is expected,",
        "not a corrupt delivery. Use whichever of the two procedures below",
        "matches what you have.",
        "",
        "### If you have the delivered PDFs",
        "",
        "Download them from the release, put them in a checkout of the delivery",
        "(the **Check out** row above) at the paths the table above gives",
        "(`docs/delivery/pdf/`), then:",
        "",
        "```bash",
        "sha256sum -c docs/delivery/SHA256SUMS",
        "```",
        "",
        "Every line must read `OK`. `SHA256SUMS` carries the same hashes in the",
        "format that command expects, so nothing has to be compared by eye.",
        "",
        "### If you have only the source",
        "",
        "Rebuild them. The PDFs are byte-reproducible: rendering them from the",
        "same sources produces the same bytes, because the only non-determinism",
        "in the pipeline — the two timestamps the PDF engine embeds — is pinned",
        "to the date each document declares.",
        "",
        "```bash",
        "python scripts/render_delivery_pdfs.py",
        "sha256sum -c docs/delivery/SHA256SUMS",
        "```",
        "",
        "All nine lines then read `OK`. This is the stronger of the two checks:",
        "it needs no trust in the delivered files at all, only in the sources",
        "the checkout holds. The hashes were produced with pandoc 3.6.3,",
        "Node 24, and Playwright 1.64.1 with its Chromium; the render script's",
        "docstring says how to provide them (`npm i --no-save playwright@1.64.1",
        "&& npx playwright install chromium` in `apps/web`, then",
        "`NODE_PATH=apps/web/node_modules`).",
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
        "are covered by `SHA256SUMS`; both files are covered by the delivery",
        "commit the **Check out** row names.",
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
        help=(
            "The tag this delivery will carry. Needed because the tag must "
            "point at the commit containing this manifest, so it cannot "
            "exist yet when the manifest is written."
        ),
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
