"""Reloading Overture buildings: a new release replaces, a re-run is a no-op,
and a wrong-city extract leaves nothing behind.

Each case is a load that used to *commit* the wrong state:

- a release bump inserted every GERS id again under a new source row, so each
  block counted every building twice;
- re-running the same load re-inserted the rows the first clip had deleted,
  committed and published them, then deleted them again — a new layer version
  and a city-wide recompute for identical data;
- the AOI mismatch was detected only after the out-of-AOI rows were committed
  under a succeeded run and a published version.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pytest
from shapely.geometry import box

from reimagina.assets.registry import seed_assets
from reimagina.pipeline.loader import SupersededFeaturesInUse
from reimagina.pipeline.overture import (
    ExtractAoiMismatch,
    bootstrap_overture_buildings_subset,
)

from tests.integration._helpers import insert_project

_AOI = (
    "POLYGON((-115.48 32.62,-115.46 32.62,-115.46 32.64,-115.48 32.64,-115.48 32.62))"
)
_ELSEWHERE = "POLYGON((-99.2 19.4,-99.1 19.4,-99.1 19.5,-99.2 19.5,-99.2 19.4))"


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import reimagina.pipeline.bronze as bronze_mod
    import reimagina.pipeline.silver as silver_mod

    monkeypatch.setattr(bronze_mod, "BRONZE_DIR", tmp_path / "bronze")
    monkeypatch.setattr(silver_mod, "SILVER_DIR", tmp_path / "silver")


@pytest.fixture(autouse=True)
def _undo_commits(migrated_db: str):
    """These loads COMMIT, which the rolling-back `conn` fixture cannot undo.

    Left behind, the published `global.building` versions made
    test_asset_publish.py fail whenever it ran after this file. Restore the
    asset graph and drop this file's sources (features cascade) afterwards.
    """
    import psycopg2

    with psycopg2.connect(migrated_db) as before, before.cursor() as cur:
        cur.execute("SELECT id, is_current, valid_to FROM curated_geo.asset_version")
        snapshot = cur.fetchall()
    before.close()
    yield
    with psycopg2.connect(migrated_db) as after, after.cursor() as cur:
        cur.execute(
            "DELETE FROM curated_geo.asset_version WHERE NOT (id = ANY(%s::uuid[]))",
            ([str(row[0]) for row in snapshot],),
        )
        for version_id, is_current, valid_to in snapshot:
            cur.execute(
                "UPDATE curated_geo.asset_version SET is_current = %s, valid_to = %s "
                "WHERE id = %s",
                (is_current, valid_to, version_id),
            )
        cur.execute(
            "DELETE FROM curated_geo.feature f USING curated_geo.data_source_catalog s "
            "WHERE s.id = f.source_id AND s.name LIKE 'Overture Buildings - %%'"
        )
    after.close()


def _extract(tmp_path: Path) -> Path:
    """Two buildings inside the AOI, one in the bbox but outside it."""
    path = tmp_path / f"extract_{uuid4().hex[:6]}.parquet"
    gpd.GeoDataFrame(
        {"id": ["gers-in-1", "gers-in-2", "gers-out"]},
        geometry=[
            box(-115.475, 32.625, -115.4749, 32.6251),
            box(-115.470, 32.630, -115.4699, 32.6301),
            box(-115.40, 32.70, -115.3999, 32.7001),
        ],
        crs="EPSG:4326",
    ).to_parquet(path)
    return path


def _aoi(conn, wkt: str = _AOI) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO curated_geo.area_of_interest (project_id, name, geometry) "
            "VALUES (%s, 'reload aoi', ST_GeomFromText(%s, 4326)) RETURNING id",
            (insert_project(conn), wkt),
        )
        return str(cur.fetchone()[0])


def _load(conn, raw: Path, *, name: str, release: str, aoi_id: str) -> dict:
    return bootstrap_overture_buildings_subset(
        conn,
        raw_path=raw,
        orchestrator_run_id=str(uuid4()),
        dataset_version=release,
        source_name=name,
        aoi_id=aoi_id,
    )


def _buildings(conn, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.feature f "
            "JOIN curated_geo.data_source_catalog s ON s.id = f.source_id "
            "WHERE f.type_slug = 'building' AND s.name = %s",
            (name,),
        )
        return int(cur.fetchone()[0])


def _building_version(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT av.version FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = 'global.building' AND av.is_current "
            "AND av.partition_key = ''"
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_a_new_release_replaces_the_previous_one(conn, tmp_path) -> None:
    name = f"Overture Buildings - reload {uuid4().hex[:6]}"
    aoi = _aoi(conn)
    conn.commit()
    _load(conn, _extract(tmp_path), name=name, release="2026-10-21.0", aoi_id=aoi)
    _load(conn, _extract(tmp_path), name=name, release="2026-11-18.0", aoi_id=aoi)
    assert _buildings(conn, name) == 2, "both releases' buildings are live"


def test_rerunning_a_release_repairs_a_doubled_database(
    conn, tmp_path, monkeypatch
) -> None:
    """A database doubled before the supersede existed is cleaned by re-running
    the current release: every row conflicts, so the area must come from the
    input file, not from what this run inserted (nothing)."""
    import reimagina.pipeline.loader as loader

    name = f"Overture Buildings - doubled {uuid4().hex[:6]}"
    aoi = _aoi(conn)
    conn.commit()
    _load(conn, _extract(tmp_path), name=name, release="2026-10-21.0", aoi_id=aoi)
    with monkeypatch.context() as m:
        m.setattr(loader, "supersede_other_versions", lambda *a, **k: 0)
        _load(conn, _extract(tmp_path), name=name, release="2026-11-18.0", aoi_id=aoi)
    assert _buildings(conn, name) == 4, "precondition: both releases are live"

    result = _load(
        conn, _extract(tmp_path), name=name, release="2026-11-18.0", aoi_id=aoi
    )

    assert _buildings(conn, name) == 2
    assert result["records_superseded"] == 2


def test_a_release_under_another_name_is_left_alone(conn, tmp_path) -> None:
    """The supersede is scoped to one source's lineage, not to the layer."""
    aoi = _aoi(conn)
    conn.commit()
    other = f"Overture Buildings - other {uuid4().hex[:6]}"
    _load(conn, _extract(tmp_path), name=other, release="2026-10-21.0", aoi_id=aoi)
    name = f"Overture Buildings - reload {uuid4().hex[:6]}"
    _load(conn, _extract(tmp_path), name=name, release="2026-11-18.0", aoi_id=aoi)
    assert _buildings(conn, other) == 2


def test_rerunning_the_same_load_publishes_nothing(conn, tmp_path) -> None:
    seed_assets(conn)
    aoi = _aoi(conn)
    conn.commit()
    name = f"Overture Buildings - rerun {uuid4().hex[:6]}"
    raw = _extract(tmp_path)
    first = _load(conn, raw, name=name, release="2026-10-21.0", aoi_id=aoi)
    version = _building_version(conn)

    second = _load(conn, raw, name=name, release="2026-10-21.0", aoi_id=aoi)

    assert first["records_written"] == 2, "the published count is net of the clip"
    assert second["records_written"] == 0
    assert _building_version(conn) == version
    assert _buildings(conn, name) == 2


def test_a_wrong_city_extract_is_rolled_back(conn, tmp_path, monkeypatch) -> None:
    seed_assets(conn)
    aoi = _aoi(conn, _ELSEWHERE)
    conn.commit()
    version = _building_version(conn)
    name = f"Overture Buildings - wrong city {uuid4().hex[:6]}"

    with pytest.raises(ExtractAoiMismatch):
        _load(conn, _extract(tmp_path), name=name, release="2026-10-21.0", aoi_id=aoi)

    assert _buildings(conn, name) == 0
    assert _building_version(conn) == version
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.status FROM curated_geo.ingestion_run r "
            "JOIN curated_geo.data_source_catalog s ON s.id = r.source_id "
            "WHERE s.name = %s",
            (name,),
        )
        assert [row[0] for row in cur.fetchall()] == ["failed"]


def test_superseding_refuses_to_drop_a_scenario_edit(conn, tmp_path) -> None:
    """Deleting a superseded building cascades to its scenario overrides —
    user data. The load refuses instead."""
    aoi = _aoi(conn)
    conn.commit()
    name = f"Overture Buildings - edited {uuid4().hex[:6]}"
    _load(conn, _extract(tmp_path), name=name, release="2026-10-21.0", aoi_id=aoi)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_core.scenario (project_id, name, aoi_id) "
            "SELECT project_id, 'edit', id FROM curated_geo.area_of_interest "
            "WHERE id = %s RETURNING id",
            (aoi,),
        )
        scenario_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO curated_geo.scenario_override "
            "(scenario_id, feature_id, attribute_key, value_numeric) "
            "SELECT %s, f.id, 'floors', 3 FROM curated_geo.feature f "
            "JOIN curated_geo.data_source_catalog s ON s.id = f.source_id "
            "WHERE s.name = %s LIMIT 1",
            (scenario_id, name),
        )
    conn.commit()

    with pytest.raises(SupersededFeaturesInUse):
        _load(conn, _extract(tmp_path), name=name, release="2026-11-18.0", aoi_id=aoi)
    assert _buildings(conn, name) == 2
