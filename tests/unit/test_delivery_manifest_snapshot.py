"""Delivery checks must identify the delivered bytes, including working changes."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/delivery_manifest.py"
DOCUMENTS = (
    "01_architecture_map",
    "03_database_and_api_reference",
    "04_technical_documentation",
    "05_capability_proof",
)


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "delivery_manifest_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in {
        "ROOT": tmp_path,
        "DELIVERY": tmp_path / "docs/delivery",
        "SUMS": tmp_path / "docs/delivery/SHA256SUMS",
        "MANIFEST": tmp_path / "docs/delivery/MANIFEST.md",
    }.items():
        monkeypatch.setattr(module, name, value)
    for stem in DOCUMENTS:
        source = tmp_path / f"docs/delivery/{stem}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('---\ndate: "20 November 2026"\n---\n')
        pdf = tmp_path / f"docs/delivery/pdf/{stem}.pdf"
        pdf.parent.mkdir(exist_ok=True)
        pdf.write_bytes(b"PDF " + stem.encode())
    (tmp_path / "docs/THIRD_PARTY_LICENSES.md").write_text("inventory")
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')
    (tmp_path / ".gitignore").write_text("docs/delivery/\n")
    (tmp_path / "app.py").write_text("original\n")
    git(tmp_path, "init")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "base",
    )
    return module


def snapshot(module):
    path = module.DELIVERY / "source/r-evo-v1.0.0.tar.gz"
    path.parent.mkdir(exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for relative in git(module.ROOT, "ls-files").splitlines():
            archive.add(module.ROOT / relative, arcname=f"r-evo-v1.0.0/{relative}")
    return str(path)


def test_untagged_checkout_requires_snapshot(bundle):
    with pytest.raises(SystemExit, match="source.archive|snapshot"):
        bundle.main([])


def test_snapshot_covers_sources_and_checks_current_working_bytes(bundle):
    (bundle.ROOT / "app.py").write_text("reviewed change\n")
    archive = snapshot(bundle)
    assert bundle.main(["--source-archive", archive]) == 0
    text = bundle.MANIFEST.read_text()
    assert "reviewed source snapshot" in text
    assert "No release publication" in text
    assert "acceptance" in text
    assert "git checkout " + git(bundle.ROOT, "rev-parse", "HEAD") in text
    assert "shasum -a 256 -c SHA256SUMS" in text
    paths = {line.split("  ", 1)[1] for line in bundle.SUMS.read_text().splitlines()}
    for stem in DOCUMENTS:
        assert f"docs/delivery/{stem}.md" in paths
        assert f"docs/delivery/pdf/{stem}.pdf" in paths
    assert "docs/delivery/source/r-evo-v1.0.0.tar.gz" in paths
    assert bundle.main(["--check", "--source-archive", archive]) == 0
    (bundle.ROOT / "app.py").write_text("changed after snapshot\n")
    with pytest.raises(SystemExit, match="snapshot|archive"):
        bundle.main(["--check", "--source-archive", archive])


def test_check_does_not_ignore_source_provenance(bundle):
    archive = snapshot(bundle)
    bundle.main(["--source-archive", archive])
    text = bundle.MANIFEST.read_text()
    bundle.MANIFEST.write_text(
        text.replace(git(bundle.ROOT, "rev-parse", "HEAD"), "0" * 40)
    )
    assert bundle.main(["--check", "--source-archive", archive]) == 1


def test_clean_tagged_default_preserves_reproducible_git_archive(bundle):
    git(bundle.ROOT, "tag", "v1.0.0")
    assert bundle.main(["--tag", "v1.0.0"]) == 0
    text = bundle.MANIFEST.read_text()
    assert "git archive --format=tar --prefix=r-evo-v1.0.0/" in text
    assert bundle.main(["--check", "--tag", "v1.0.0"]) == 0
    (bundle.ROOT / "app.py").write_text("dirty\n")
    with pytest.raises(SystemExit, match="snapshot|source.archive|clean"):
        bundle.main(["--tag", "v1.0.0"])


def test_explicit_tag_must_resolve_to_head(bundle):
    git(bundle.ROOT, "tag", "v1.0.0")
    with pytest.raises(SystemExit, match="tag|HEAD"):
        bundle.main(["--tag", "missing-tag", "--source-archive", snapshot(bundle)])


def test_snapshot_check_rejects_files_deleted_after_packaging(bundle):
    archive = snapshot(bundle)
    bundle.main(["--source-archive", archive])
    (bundle.ROOT / "app.py").unlink()
    with pytest.raises(SystemExit, match="snapshot|archive"):
        bundle.main(["--check", "--source-archive", archive])


def test_snapshot_checks_included_new_source_files(bundle):
    new_source = bundle.ROOT / "new_test.py"
    new_source.write_text("new regression\n")
    archive = snapshot(bundle)
    with tarfile.open(archive, "r:gz") as original:
        entries = [(m, original.extractfile(m).read()) for m in original if m.isfile()]
    with tarfile.open(archive, "w:gz") as updated:
        for member, content in entries:
            updated.addfile(member, io.BytesIO(content))
        updated.add(new_source, arcname="r-evo-v1.0.0/new_test.py")
    bundle.main(["--source-archive", archive])
    new_source.write_text("changed regression\n")
    with pytest.raises(SystemExit, match="snapshot|archive"):
        bundle.main(["--check", "--source-archive", archive])
