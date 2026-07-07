from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from reimagina.data.feature import FEATURE_TYPE_ROWS
from reimagina.pipeline.overture import (
    OVERTURE_BUILDINGS_MEXICO_CITY,
    OVERTURE_DEFAULT_RELEASE,
    OVERTURE_PENDING_STORAGE_URI,
    OVERTURE_BUILDINGS_SOURCE_NAME,
    ensure_overture_buildings_source,
    extract_overture_buildings_subset,
    overture_theme_type_glob,
    bootstrap_overture_buildings_subset,
)


def test_overture_theme_type_glob_uses_pinned_release():
    glob = overture_theme_type_glob()
    # Default release is whatever OVERTURE_DEFAULT_RELEASE is set to —
    # don't pin a literal here so bumping the constant doesn't drag a
    # test edit along with it. Verify shape only.
    assert f"/release/{OVERTURE_DEFAULT_RELEASE}/" in glob
    assert "theme=buildings/type=building/*.parquet" in glob
    assert glob.startswith("s3://")


def test_overture_source_spec_matches_first_source_choice():
    assert OVERTURE_BUILDINGS_MEXICO_CITY.name == OVERTURE_BUILDINGS_SOURCE_NAME
    assert OVERTURE_BUILDINGS_MEXICO_CITY.storage_type == "vector"
    assert OVERTURE_BUILDINGS_MEXICO_CITY.integration_role == "canonical_entity"
    assert OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug == "building"


def test_building_feature_type_is_seeded():
    assert (
        "global",
        "building",
        "Building",
        "polygon",
        "context",
        None,
    ) in FEATURE_TYPE_ROWS


def test_ensure_overture_source_returns_existing_id(monkeypatch):
    monkeypatch.setattr(
        "reimagina.pipeline.overture.cat.get_source",
        lambda conn, **kwargs: {
            "id": "existing-source-id",
            "storage_uri": OVERTURE_PENDING_STORAGE_URI,
        },
    )

    source_id = ensure_overture_buildings_source(object())

    assert source_id == "existing-source-id"


def test_extract_overture_buildings_subset_filters_local_geoparquet(tmp_path):
    source_path = tmp_path / "source.parquet"
    output_path = tmp_path / "subset.parquet"
    gdf = gpd.GeoDataFrame(
        [
            {
                "id": "inside",
                "geometry": Polygon(
                    [(-99.1, 19.2), (-99.0, 19.2), (-99.0, 19.3), (-99.1, 19.3)]
                ),
            },
            {
                "id": "outside",
                "geometry": Polygon(
                    [(-90.1, 10.2), (-90.0, 10.2), (-90.0, 10.3), (-90.1, 10.3)]
                ),
            },
        ],
        crs="EPSG:4326",
    )
    gdf.to_parquet(source_path)

    extracted = extract_overture_buildings_subset(
        output_path=output_path,
        source_glob=str(source_path),
    )

    subset = gpd.read_parquet(extracted)
    assert extracted == output_path
    assert list(subset["id"]) == ["inside"]


def test_bootstrap_flow_seeds_feature_types_before_loading(monkeypatch, tmp_path):
    calls: list[str] = []

    class FakeConn:
        def commit(self):
            calls.append("commit")

    monkeypatch.setattr(
        "reimagina.pipeline.overture.feat.seed_feature_types",
        lambda conn: calls.append("seed_feature_types"),
    )
    monkeypatch.setattr(
        "reimagina.pipeline.overture.ensure_overture_buildings_source",
        lambda conn, **kwargs: "source-1",
    )
    monkeypatch.setattr(
        "reimagina.pipeline.overture.stage_overture_buildings_subset",
        lambda raw_path, **kwargs: (
            tmp_path / "bronze.parquet",
            tmp_path / "silver.parquet",
        ),
    )
    monkeypatch.setattr(
        "reimagina.pipeline.overture.cat.update_source_storage_uri",
        lambda conn, source_id, storage_uri: calls.append("update_source_storage_uri"),
    )
    monkeypatch.setattr(
        "reimagina.pipeline.overture.load_overture_buildings_subset",
        lambda conn, **kwargs: {"records_written": 1, "ingestion_run_id": "run-1"},
    )

    result = bootstrap_overture_buildings_subset(
        FakeConn(),
        raw_path=tmp_path / "raw.parquet",
        orchestrator_run_id="orch-1",
    )

    assert calls[0] == "seed_feature_types"
    assert result["source_id"] == "source-1"
