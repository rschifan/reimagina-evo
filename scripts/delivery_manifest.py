"""Identify delivery artefacts and source bytes with SHA-256.

Use --source-archive PATH for a reviewed working snapshot. Without it, a clean
checkout at an existing tag is required and source is identified by reproducible
uncompressed git archive bytes. Neither mode publishes a release or establishes
client acceptance. Generated files and excluded document sources travel with the
bundle, preserving repository-relative paths.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "docs" / "delivery"
SUMS = DELIVERY / "SHA256SUMS"
MANIFEST = DELIVERY / "MANIFEST.md"

DOCUMENTS = (
    ("01_architecture_map", "Work 1 — architecture map"),
    ("03_database_and_api_reference", "Work 3 — database and API reference"),
    ("04_technical_documentation", "Work 4 — technical documentation"),
    ("05_capability_proof", "Annex — capability evidence"),
)
SOURCES = tuple(f"docs/delivery/{stem}.md" for stem, _ in DOCUMENTS)
ARTEFACTS = (
    *((f"docs/delivery/pdf/{stem}.pdf", work) for stem, work in DOCUMENTS),
    *((f"docs/delivery/{stem}.md", f"{work} (source)") for stem, work in DOCUMENTS),
    ("docs/THIRD_PARTY_LICENSES.md", "Article 4.1 — dependency inventory"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _version(explicit: str | None) -> str:
    if explicit:
        return explicit.removeprefix("v")
    found = re.search(
        r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M
    )
    return found.group(1) if found else "(unknown)"


def _source_archive(commit: str, version: str) -> tuple[str, int]:
    tar = subprocess.run(
        ["git", "archive", "--format=tar", f"--prefix=r-evo-v{version}/", commit],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.sha256(tar).hexdigest(), len(tar)


def _verify_snapshot(path: Path) -> None:
    """Compare archived tracked files to disk without extracting untrusted paths."""
    tracked = _git("ls-files", "-z").split("\0")
    wanted = {name for name in tracked if name and (ROOT / name).exists()}
    try:
        with tarfile.open(path) as archive:
            files = {}
            for member in archive.getmembers():
                parts = PurePosixPath(member.name).parts
                if member.isdir():
                    continue
                if len(parts) < 2 or ".." in parts or parts[0] == "/":
                    raise SystemExit(
                        "source snapshot archive has an unsafe or unprefixed path"
                    )
                relative = str(PurePosixPath(*parts[1:]))
                if relative in files:
                    raise SystemExit(f"source snapshot archive repeats {relative}")
                files[relative] = member
            for name in sorted(wanted | set(files)):
                archived = files.get(name)
                local = ROOT / name
                if archived is None:
                    raise SystemExit(f"source snapshot archive is missing {name}")
                if not local.exists() and not local.is_symlink():
                    raise SystemExit(
                        f"source snapshot archive contains deleted file: {name}"
                    )
                if local.is_symlink():
                    matches = archived.issym() and archived.linkname == os.readlink(
                        local
                    )
                elif archived.isfile():
                    stream = archive.extractfile(archived)
                    assert stream is not None
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        digest.update(chunk)
                    matches = digest.hexdigest() == _sha256(local)
                    matches = matches and bool(archived.mode & 0o111) == bool(
                        local.stat().st_mode & 0o111
                    )
                else:
                    matches = False
                if not matches:
                    raise SystemExit(
                        f"source snapshot archive differs from working file: {name}"
                    )
    except (OSError, tarfile.TarError) as exc:
        raise SystemExit(f"cannot read source snapshot archive: {exc}") from exc


def _delivery_date() -> str:
    declared = set()
    for relative in SOURCES:
        path = ROOT / relative
        if path.is_file():
            for line in path.read_text()[:600].splitlines():
                if line.startswith("date:"):
                    declared.add(line.split(":", 1)[1].strip().strip('"').strip("'"))
                    break
    if len(declared) > 1:
        raise SystemExit(
            f"the delivery documents declare different dates: {sorted(declared)}"
        )
    return declared.pop() if declared else "(no date declared)"


def _rows() -> list[tuple[str, str, int, str]]:
    rows = []
    missing = []
    for relative, work in ARTEFACTS:
        path = ROOT / relative
        if path.is_file():
            rows.append((relative, _sha256(path), path.stat().st_size, work))
        else:
            missing.append(relative)
    if missing:
        raise SystemExit(
            "cannot build a manifest with artefacts missing; render PDFs first:\n  "
            + "\n  ".join(missing)
        )
    return rows


def _sums(rows: list[tuple[str, str, int, str]]) -> str:
    return "".join(f"{digest}  {relative}\n" for relative, digest, _, _ in rows)


def _manifest(
    rows: list[tuple[str, str, int, str]],
    date: str,
    tag: str | None = None,
    version: str = "(unknown)",
    source_archive: Path | None = None,
) -> str:
    commit = _git("rev-parse", "HEAD")
    if not commit:
        raise SystemExit(
            "source provenance requires a git checkout and source snapshot"
        )
    if tag and _git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit:
        raise SystemExit("the supplied tag must exist and resolve to HEAD")
    tag = tag or _git("describe", "--tags", "--exact-match") or "(untagged)"
    dirty = bool(_git("status", "--porcelain", "--untracked-files=normal"))
    if source_archive is None and (dirty or tag == "(untagged)"):
        raise SystemExit(
            "a clean tagged checkout is required; use --source-archive for a reviewed source snapshot"
        )
    if source_archive:
        _verify_snapshot(source_archive)
        relative = source_archive.relative_to(ROOT).as_posix()
        rows.append(
            (
                relative,
                _sha256(source_archive),
                source_archive.stat().st_size,
                "Work 2 — reviewed source snapshot",
            )
        )
        source_description = (
            "The supplied archive contains the reviewed working source snapshot. "
            "The base commit alone does not reproduce working changes. Extract "
            "the archive into a separate directory; its top-level directory holds "
            "the source to use, including changes relative to the base commit."
        )
        checkout = f"`git checkout {commit}` for the base; then use the supplied source snapshot (required)."
        state = "reviewed source snapshot"
    else:
        src_sha, src_size = _source_archive(commit, version)
        source_description = (
            f"Source is the clean tagged commit's uncompressed `git archive` tar "
            f"({src_size:,} bytes), SHA-256 `{src_sha}`. Recompute from the clone:\n\n"
            f"```bash\ngit archive --format=tar --prefix=r-evo-v{version}/ {commit} | sha256sum\n```\n\n"
            f"For the release bundle, `make release VERSION={tag}` creates "
            f"`r-evo-v{version}.tar.gz` with `gzip -n`; its compressed hash is in "
            "the generated `SHA256SUMS-assets`."
        )
        checkout = f"`git checkout {tag}`"
        state = "clean tagged source"
    lines = [
        "# Delivery manifest",
        "",
        "Prepared for client review under Articles 2.1 and 2.2.",
        "No release publication or client acceptance is asserted by this manifest.",
        "",
        "| | |",
        "|---|---|",
        f"| **Date** | {date} |",
        f"| **Version** | {version} |",
        f"| **Tag** | `{tag}` |",
        f"| **Generated from commit** | `{commit}` |",
        f"| **Base commit** | `{commit}` |",
        f"| **Source state** | {state} |",
        f"| **Working tree** | {'dirty' if dirty else 'clean'} |",
        f"| **Check out** | {checkout} |",
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
        source_description,
        "",
        "## Verifying this",
        "",
        "The delivery PDFs and document sources are **not in the repository**.",
        "They travel with this client bundle, together with the source archive,",
        "`MANIFEST.md`, `SHA256SUMS` and `docs/THIRD_PARTY_LICENSES.md`.",
        "All four excluded sources travel at the paths listed in the table:",
        *(f"- `{relative}`" for relative in SOURCES),
        "",
        "Use the supplied snapshot's source root (or the tagged checkout for a",
        "tagged release). Restore every bundle file at its repository-relative",
        "path, including `docs/delivery/pdf/` and `docs/delivery/source/` when supplied.",
        "Put a copy of `SHA256SUMS` at that root and run:",
        "",
        "```bash",
        "sha256sum -c SHA256SUMS",
        "# macOS alternative:",
        "shasum -a 256 -c SHA256SUMS",
        "```",
        "",
        "Every listed file must be present and report OK after reconstruction.",
        "",
        "Hashes detect byte changes; they are not signatures and do not establish",
        "authorship or acceptance. Neither generated metadata file covers itself.",
        "",
        "Generated by `scripts/delivery_manifest.py`. Do not edit by hand.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--version", default=None)
    parser.add_argument(
        "--source-archive",
        type=Path,
        help="Existing working source tar archive within the repository root.",
    )
    parser.add_argument("--print-commit", action="store_true")
    args = parser.parse_args(argv)
    if args.print_commit:
        print(_git("rev-parse", "HEAD"))
        return 0
    archive = None
    if args.source_archive:
        archive = args.source_archive.resolve()
        if not archive.is_relative_to(ROOT.resolve()) or not archive.is_file():
            raise SystemExit(
                "source archive must be an existing file within the repository root"
            )
    rows = _rows()
    manifest = _manifest(
        rows, _delivery_date(), args.tag, _version(args.version), archive
    )
    sums = _sums(rows)
    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, want in ((SUMS, sums), (MANIFEST, manifest))
            if not path.is_file() or path.read_text() != want
        ]
        if stale:
            print("stale: " + ", ".join(str(s) for s in stale), file=sys.stderr)
            return 1
        print(f"{SUMS.relative_to(ROOT)} and {MANIFEST.relative_to(ROOT)} are current.")
        return 0
    SUMS.write_text(sums)
    MANIFEST.write_text(manifest)
    print(
        f"wrote {SUMS.relative_to(ROOT)} and {MANIFEST.relative_to(ROOT)} — {len(rows)} artefacts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
