"""``build_mexicali_bundle.py fetch``: checksum before extraction, safe
extraction, reuse of what is already there. A tiny synthetic archive served
from a file:// URL stands in for the release asset; no network.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_mexicali_bundle as bb  # noqa: E402


def _archive(path: Path, extra: tuple[str, bytes] | None = None) -> str:
    """A bundle archive whose SHA256SUMS matches; returns its SHA-256."""
    data = b"rows"
    members = {
        f"{bb.BUNDLE_TOP}/osm/edges.parquet": data,
        f"{bb.BUNDLE_TOP}/{bb.SUMS}": (
            f"{hashlib.sha256(data).hexdigest()}  osm/edges.parquet\n".encode()
        ),
    }
    if extra:
        members[extra[0]] = extra[1]
    with tarfile.open(path, "w:gz") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def served(tmp_path, monkeypatch):
    src = tmp_path / "served.tar.gz"
    monkeypatch.setattr(bb, "BUNDLE_SHA256", _archive(src))
    return src.as_uri(), tmp_path / "cache"


def test_fetch_downloads_checks_and_extracts(served):
    url, cache = served
    bundle = bb.fetch(cache, url)
    assert bundle == cache / bb.BUNDLE_VERSION / bb.BUNDLE_TOP
    assert bb.verify(bundle) == []
    assert not list(bundle.parent.glob("*.part"))


def test_checksum_mismatch_is_refused_and_nothing_extracted(served, monkeypatch):
    url, cache = served
    monkeypatch.setattr(bb, "BUNDLE_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="does not match the pinned checksum"):
        bb.fetch(cache, url)
    assert [p.name for p in (cache / bb.BUNDLE_VERSION).iterdir()] == []


def test_path_traversal_member_is_refused(tmp_path, monkeypatch):
    src = tmp_path / "evil.tar.gz"
    monkeypatch.setattr(bb, "BUNDLE_SHA256", _archive(src, ("../evil", b"x")))
    cache = tmp_path / "cache"
    with pytest.raises(SystemExit, match="refused to extract"):
        bb.fetch(cache, src.as_uri())
    assert not (cache / "evil").exists()
    assert not (cache / bb.BUNDLE_VERSION / bb.BUNDLE_TOP).exists()


def test_second_call_reuses_the_extracted_bundle(served):
    url, cache = served
    bundle = bb.fetch(cache, url)
    stamp = (bundle / bb.SUMS).stat().st_mtime_ns
    assert bb.fetch(cache, "file:///nonexistent.tar.gz") == bundle
    assert (bundle / bb.SUMS).stat().st_mtime_ns == stamp


def test_cached_archive_is_reused_without_downloading(served):
    url, cache = served
    bundle = bb.fetch(cache, url)
    (bundle / "osm/edges.parquet").write_bytes(b"tampered")  # fails verify
    assert bb.fetch(cache, "file:///nonexistent.tar.gz") == bundle
    assert bb.verify(bundle) == []


def test_unreachable_url_points_at_the_manual_route(tmp_path):
    with pytest.raises(SystemExit, match="--bundle DIR"):
        bb.fetch(tmp_path, "file:///nonexistent.tar.gz")
