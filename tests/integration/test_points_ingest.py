"""Integration tests for reimagina.pipeline.points.bootstrap.

Builds tiny synthetic GeoParquet files that mirror the on-the-data-lake
amenity schema (``id, category, capacity, geometry, fuente, fecha,
entidad``), runs ``bootstrap()`` against the migrated test DB, and
asserts features land with the right type, attributes, and geometry.

Test isolation: ``points.bootstrap()`` commits its source + feature
inserts (so production re-runs are durable). To prevent state from
one test leaking into the next, each test patches the spec's
``version`` to a unique string so each run creates its own
data_source_catalog row.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pytest
from shapely.geometry import Point

from reimagina.pipeline import points


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_spec_versions(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Patch every PointsLayerSpec.version with a unique test-scoped value.

    PointsLayerSpec is a frozen dataclass, so we replace each entry in
    LAYER_SPECS with a copy whose version differs. The aliases at the
    module top (TLALPAN_AMENITIES, MEXICALI_AMENITIES) get replaced too
    — bootstrap() reads from LAYER_SPECS, so that's the only path that
    matters.
    """
    import dataclasses

    versioned = {
        key: f"test-{uuid4().hex[:8]}"
        for key in points.LAYER_SPECS
    }
    fresh: dict[str, points.PointsLayerSpec] = {
        key: dataclasses.replace(spec, version=versioned[key])
        for key, spec in points.LAYER_SPECS.items()
    }
    monkeypatch.setattr(points, "LAYER_SPECS", fresh)
    return versioned


@pytest.fixture
def isolated_silver_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``silver.save_silver_vector`` writes into ``tmp_path``."""
    import reimagina.pipeline.silver as silver_mod
    silver_dir = tmp_path / "silver"
    silver_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    return silver_dir


@pytest.fixture
def fake_amenity_root(tmp_path: Path) -> Path:
    """Lay down both pilot-city geoparquets at the locations the specs
    expect (relative to ``source_root``)."""
    root = tmp_path / "azure"
    _write_amenity_geoparquet(
        root / "sigcdmx" / "amenities" / "exe_amenities_tlalpan_complete.geoparquet",
        rows=[
            {"id": "T1", "category": "Farmacia", "capacity": 0,
             "lon": -99.18, "lat": 19.30, "fuente": "sigcdmx", "entidad": 9},
            {"id": "T2", "category": "Consultorio medico", "capacity": 4,
             "lon": -99.19, "lat": 19.31, "fuente": "DENUE", "entidad": 9},
        ],
    )
    _write_amenity_geoparquet(
        root / "exe" / "002_mexicali" / "equipamientos.geoparquet",
        rows=[
            {"id": "M1", "category": "Guarderia", "capacity": 30,
             "lon": -115.45, "lat": 32.65, "fuente": "DENUE", "entidad": 2},
        ],
    )
    return root


# ─── Helpers ──────────────────────────────────────────────────────────


def _write_amenity_geoparquet(path: Path, *, rows: list[dict]) -> None:
    """Match the real amenity schema exactly so the ingest sees the
    columns the spec.attributes_cols list declares."""
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame(
        {
            "id": [r["id"] for r in rows],
            "category": [r["category"] for r in rows],
            "capacity": [r["capacity"] for r in rows],
            "geometry": [Point(r["lon"], r["lat"]) for r in rows],
            "fuente": [r["fuente"] for r in rows],
            "fecha": [date(2026, 3, 1) for _ in rows],
            "entidad": [r["entidad"] for r in rows],
        },
        crs="EPSG:4326",
    )
    gdf.to_parquet(path)


def _fetch_features(conn, *, dataset_version: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.name,
                   f.attributes,
                   f.dataset_version,
                   ftc.type_slug,
                   ST_X(f.geometry) AS lon,
                   ST_Y(f.geometry) AS lat
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog ftc ON ftc.id = f.type_id
            WHERE f.dataset_version = %s
            ORDER BY f.source_record_ref
            """,
            (dataset_version,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─── Tests ────────────────────────────────────────────────────────────


def test_bootstrap_loads_each_pilot_layer(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    result = points.bootstrap(
        conn,
        source_root=fake_amenity_root,
    )
    assert result["layers_loaded"] == ["tlalpan_amenities", "mexicali_amenities"]
    assert {row["records_written"] for row in result["layers"]} == {1, 2}


def test_bootstrap_lands_features_as_global_establishment(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    points.bootstrap(
        conn, layers=["tlalpan_amenities"], source_root=fake_amenity_root,
    )
    rows = _fetch_features(conn, dataset_version=isolated_spec_versions["tlalpan_amenities"])

    assert len(rows) == 2
    assert {row["type_slug"] for row in rows} == {"establishment"}


def test_bootstrap_writes_attributes_to_jsonb(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    points.bootstrap(
        conn, layers=["tlalpan_amenities"], source_root=fake_amenity_root,
    )
    rows = _fetch_features(conn, dataset_version=isolated_spec_versions["tlalpan_amenities"])
    by_id = {r["name"]: r for r in rows}
    farmacia = by_id["Farmacia"]
    assert farmacia["attributes"]["category"] == "Farmacia"
    assert farmacia["attributes"]["fuente"] == "sigcdmx"
    # entidad is an int in the source; jsonb preserves the type.
    assert farmacia["attributes"]["entidad"] == 9


def test_bootstrap_preserves_point_geometry(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    points.bootstrap(
        conn, layers=["mexicali_amenities"], source_root=fake_amenity_root,
    )
    rows = _fetch_features(conn, dataset_version=isolated_spec_versions["mexicali_amenities"])
    assert len(rows) == 1
    # Coordinates land in EPSG:4326, in the same hemisphere/quadrant
    # as the source — i.e. WGS-84 wasn't dropped or swapped.
    assert rows[0]["lon"] == pytest.approx(-115.45, rel=1e-6)
    assert rows[0]["lat"] == pytest.approx(32.65, rel=1e-6)


def test_bootstrap_carries_azure_lineage_in_storage_uri(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    result = points.bootstrap(
        conn, layers=["mexicali_amenities"], source_root=fake_amenity_root,
    )
    source_id = result["layers"][0]["source_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_uri FROM curated_geo.data_source_catalog WHERE id = %s",
            (source_id,),
        )
        (storage_uri,) = cur.fetchone()
    assert storage_uri == (
        f"{points.AZURE_RAW_PREFIX}/exe/002_mexicali/equipamientos.geoparquet"
    )


def test_bootstrap_is_idempotent_on_source_registration(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    first = points.bootstrap(
        conn, layers=["tlalpan_amenities"], source_root=fake_amenity_root,
    )
    second = points.bootstrap(
        conn, layers=["tlalpan_amenities"], source_root=fake_amenity_root,
    )
    assert first["layers"][0]["source_id"] == second["layers"][0]["source_id"]


def test_bootstrap_rejects_unknown_layer(
    conn, fake_amenity_root: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    with pytest.raises(ValueError, match="Unknown amenity layer"):
        points.bootstrap(
            conn,
            layers=["tlalpan_amenities", "made_up"],
            source_root=fake_amenity_root,
        )


def test_bootstrap_raises_when_source_file_missing(
    conn, tmp_path: Path, isolated_silver_dir: Path,
    isolated_spec_versions: dict[str, str],
):
    """Pointing source_root at an empty directory must fail loudly rather
    than silently writing zero rows."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="Amenity GeoParquet not found"):
        points.bootstrap(
            conn, layers=["tlalpan_amenities"], source_root=empty,
        )
