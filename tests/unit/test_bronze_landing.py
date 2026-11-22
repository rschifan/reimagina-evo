"""Bronze files are immutable: landing never overwrites a different file."""

from __future__ import annotations

from pathlib import Path

import pytest

import reimagina.pipeline.bronze as bronze


@pytest.fixture(autouse=True)
def _bronze_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bronze, "BRONZE_DIR", tmp_path / "bronze")
    return tmp_path / "bronze"


def _raw(tmp_path: Path, folder: str, name: str, body: bytes) -> Path:
    path = tmp_path / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_a_second_extract_under_the_same_name_keeps_the_first(tmp_path: Path) -> None:
    first = bronze.land_file(_raw(tmp_path, "a", "places.parquet", b"mexicali"), "s1")
    second = bronze.land_file(_raw(tmp_path, "b", "places.parquet", b"tlalpan"), "s1")

    assert first.read_bytes() == b"mexicali"
    assert second.read_bytes() == b"tlalpan"
    assert first != second


def test_relanding_the_same_bytes_reuses_the_copy(tmp_path: Path) -> None:
    first = bronze.land_file(_raw(tmp_path, "a", "places.parquet", b"same"), "s1")
    again = bronze.land_file(_raw(tmp_path, "b", "places.parquet", b"same"), "s1")
    assert first == again


def test_a_shapefile_family_lands_in_one_folder(tmp_path: Path) -> None:
    bronze.land_file(
        _raw(tmp_path, "a", "roads.shp", b"v1"),
        "s1",
        sidecars=[_raw(tmp_path, "a", "roads.dbf", b"v1")],
    )
    main = bronze.land_file(
        _raw(tmp_path, "b", "roads.shp", b"v2"),
        "s1",
        sidecars=[_raw(tmp_path, "b", "roads.dbf", b"v2")],
    )
    assert (main.parent / "roads.dbf").read_bytes() == b"v2"


def test_a_changed_sidecar_lands_the_family_as_a_new_version(tmp_path: Path) -> None:
    """Same geometry, re-exported attributes: the .dbf alone differs. The whole
    family goes to a new folder; the first copy is untouched."""
    first = bronze.land_file(
        _raw(tmp_path, "a", "roads.shp", b"geom"),
        "s1",
        sidecars=[_raw(tmp_path, "a", "roads.dbf", b"attrs-v1")],
    )
    second = bronze.land_file(
        _raw(tmp_path, "b", "roads.shp", b"geom"),
        "s1",
        sidecars=[_raw(tmp_path, "b", "roads.dbf", b"attrs-v2")],
    )
    assert second.parent != first.parent
    assert (first.parent / "roads.dbf").read_bytes() == b"attrs-v1"
    assert (second.parent / "roads.dbf").read_bytes() == b"attrs-v2"
    assert second.read_bytes() == b"geom"
