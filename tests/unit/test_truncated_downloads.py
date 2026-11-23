"""A download the server cut short is never cached as complete.

``HTTPResponse.read()`` returns what arrived when the connection drops
instead of raising, so the only signal is a body shorter than its
Content-Length; a read timeout mid-body raises ``TimeoutError``, which is an
``OSError`` but not a ``URLError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reimagina.pipeline import inegi, worldpop


class _Response:
    status = 200

    def __init__(self, body: bytes, *, length: int, fail: BaseException | None = None):
        self._chunks = [body]
        self._fail = fail
        self.headers = {"Content-Length": str(length)}

    def read(self, *_args, **_kwargs) -> bytes:
        if self._chunks:
            return self._chunks.pop()
        if self._fail is not None:
            raise self._fail
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, response: _Response) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: response)


@pytest.fixture
def _worldpop_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = worldpop.WorldPopMetadata(
        dataset="wpgp", country="MEX", year=2020, url="https://x/mex.tif", title="t"
    )
    monkeypatch.setattr(worldpop, "discover_geotiff_url", lambda *_a, **_k: meta)


def _worldpop(tmp_path: Path) -> None:
    worldpop.fetch_country_raster(country="MEX", year=2020, cache_dir=tmp_path)


def test_worldpop_short_body_is_not_cached(monkeypatch, tmp_path, _worldpop_meta):
    _serve(monkeypatch, _Response(b"half", length=8))
    with pytest.raises(worldpop.WorldPopFetchError):
        _worldpop(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_worldpop_full_body_is_cached(monkeypatch, tmp_path, _worldpop_meta):
    _serve(monkeypatch, _Response(b"complete", length=8))
    _worldpop(tmp_path)
    assert [p.read_bytes() for p in tmp_path.iterdir()] == [b"complete"]


@pytest.mark.parametrize(
    "response",
    [
        _Response(b"half", length=8),
        _Response(b"half", length=8, fail=TimeoutError("read timed out")),
    ],
    ids=["short-body", "read-timeout"],
)
def test_inegi_partial_download_is_not_cached(monkeypatch, tmp_path, response):
    _serve(monkeypatch, response)
    with pytest.raises(inegi.InegiFetchError):
        inegi.cached_download("https://x/a.zip", dest_dir=tmp_path, filename="a.zip")
    assert list(tmp_path.iterdir()) == []
