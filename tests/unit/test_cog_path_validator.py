"""`/cog` path validator: containment is decided before existence.

A path outside the data root must answer the same way whether or not it
exists, so the endpoint cannot be used to probe the host filesystem, and no
answer may echo a host path back to the client.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from apps.api.cog_security import validated_cog_path


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("REIMAGINA_DATA_DIR", str(root))
    return root


def _status(url: str) -> tuple[int, str]:
    with pytest.raises(HTTPException) as exc:
        validated_cog_path(url)
    return exc.value.status_code, str(exc.value.detail)


def test_outside_paths_answer_alike_whether_or_not_they_exist(data_root, tmp_path):
    present = tmp_path / "secret.txt"
    present.write_text("x")
    absent = tmp_path / "missing.txt"

    assert _status(f"file://{present}")[0] == _status(f"file://{absent}")[0] == 403


def test_a_file_used_as_a_directory_is_a_clean_rejection(data_root, tmp_path):
    a_file = tmp_path / "secret.txt"
    a_file.write_text("x")

    assert _status(f"file://{a_file}/x")[0] == 403


def test_a_missing_file_inside_the_root_is_404(data_root):
    assert _status(f"file://{data_root}/nope.tif")[0] == 404


def test_no_answer_echoes_a_host_path(data_root, tmp_path):
    for url in (
        f"file://{tmp_path}/secret.txt",
        f"file://{data_root}/nope.tif",
        f"file://{data_root}",
    ):
        _code, detail = _status(url)
        assert str(tmp_path) not in detail


def test_a_file_inside_the_root_is_served(data_root):
    cog = data_root / "a.tif"
    cog.write_bytes(b"")

    assert validated_cog_path(f"file://{cog}") == str(cog.resolve())


def test_a_nul_byte_in_the_path_is_a_400(data_root):
    # `Path.resolve()` raises ValueError on an embedded NUL; uncaught it was a
    # 500 with a traceback logged per request.
    assert _status(f"file://{data_root}/a%00b.tif")[0] == 400
