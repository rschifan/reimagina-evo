from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point, Polygon

from reimagina.data import aoi as aoi_mod
from reimagina.data import catalog as cat
from reimagina.data import explore
from reimagina.data import feature as feat
from reimagina.data import observation as obs
from reimagina.data import overlap as ovlp
from reimagina.data import serving
from reimagina.db import bootstrap_local_platform, connect, verify_required_relations
from reimagina.pipeline.loader import load_vector_source


def _duckdb_dsn(path: Path) -> str:
    return f"duckdb:////{path}"


def test_duckdb_backend_bootstrap_and_basic_serving_flow(tmp_path):
    app_path = tmp_path / "reimagina.duckdb"
    test_path = tmp_path / "reimagina_test.duckdb"

    result = bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path),
        test_dsn=_duckdb_dsn(test_path),
    )

    assert result["app"]["migrated"] is True
    assert result["test"]["migrated"] is True

    with connect(_duckdb_dsn(app_path)) as conn:
        checks = verify_required_relations(conn)
        assert all(checks.values())
        explore_checks = verify_required_relations(
            conn,
            [
                "explore.layer_catalog_v",
                "explore.source_runs_v",
                "explore.feature_type_stats_v",
                "explore.serving_stats_v",
            ],
        )
        assert all(explore_checks.values())

        feat.seed_feature_types(conn)

        source_id = cat.register_source(
            conn,
            name="duckdb_buildings",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/buildings.geojson",
            version="v1",
        )
        run_id = cat.start_ingestion_run(
            conn,
            source_id=source_id,
            orchestrator="pytest",
            orchestrator_run_id=str(uuid4()),
            transformation_step="silver_to_curated",
            code_version="test",
        )
        type_id = feat.get_type_id(conn, "global", "building")

        gdf = gpd.GeoDataFrame(
            [
                {
                    "id": "building-1",
                    "class": "residential",
                    "names": np.array([{"primary": "Tower One"}], dtype=object),
                    "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                }
            ],
            crs="EPSG:4326",
        )
        inserted = feat.load_features(
            conn,
            gdf,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=type_id,
            dataset_version="v1",
            source_record_ref_col="id",
            attributes_cols=["class", "names"],
        )
        assert inserted == 1

        feature_id = conn.execute(
            "SELECT CAST(id AS VARCHAR) FROM curated_geo.feature WHERE source_record_ref = ?",
            ["building-1"],
        ).fetchone()[0]
        obs.upsert_observation(
            conn,
            feature_id=feature_id,
            attribute_key="status",
            value_type="text",
            value="active",
            source_id=source_id,
            ingestion_run_id=run_id,
        )

        page = serving.get_features(conn, type_slug="building")
        rows = page["features"]
        assert page["next_cursor"] is None
        assert len(rows) == 1
        assert rows[0]["type_slug"] == "building"
        assert rows[0]["attributes"]["class"] == "residential"
        assert rows[0]["attributes"]["names"] == [{"primary": "Tower One"}]
        assert rows[0]["attributes"]["status"] == "active"

        layer_rows = explore.get_layers(conn)
        assert any(row["logical_name"] == "serving_features" for row in layer_rows)

        feature_stats = explore.get_feature_stats(conn)
        assert any(row["type_slug"] == "building" and row["row_count"] == 1 for row in feature_stats)

        serving_stats = explore.get_serving_stats(conn)
        assert any(row["stat_scope"] == "all" and row["row_count"] == 1 for row in serving_stats)


def test_duckdb_parquet_fast_path_tolerates_missing_optional_columns(tmp_path):
    app_path = tmp_path / "reimagina.duckdb"
    test_path = tmp_path / "reimagina_test.duckdb"
    parquet_path = tmp_path / "buildings.parquet"

    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path),
        test_dsn=_duckdb_dsn(test_path),
    )

    gdf = gpd.GeoDataFrame(
        [
            {
                "id": "building-1",
                "class": "residential",
                "height": np.nan,
                "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            }
        ],
        crs="EPSG:4326",
    )
    gdf.to_parquet(parquet_path)

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        source_id = cat.register_source(
            conn,
            name="duckdb_buildings_parquet",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri=str(parquet_path),
            version="v1",
        )
        type_id = feat.get_type_id(conn, "global", "building")

        result = load_vector_source(
            conn,
            source_id=source_id,
            silver_path=parquet_path,
            type_id=type_id,
            dataset_version="v1",
            source_record_ref_col="id",
            name_col="name",
            attributes_cols=["class", "height", "names"],
            orchestrator_run_id=str(uuid4()),
            code_version="test",
        )

        assert result["records_written"] == 1
        row = conn.execute(
            """
            SELECT CAST(attributes AS VARCHAR)
            FROM curated_geo.feature
            WHERE source_record_ref = ?
            """,
            ["building-1"],
        ).fetchone()
        assert row is not None
        assert '"class":"residential"' in row[0]
        assert '"height":null' in row[0]
        assert "names" not in row[0]


def test_duckdb_aoi_overlaps_polygon_line_point(tmp_path):
    """AOI compute_overlaps returns metric measurements (m² / m), not degrees.

    The AOI fully contains a polygon, a line, and a point feature.
    Validates that:
    - overlap_basis is derived from feature geometry type
    - overlap_ratio is in (0, 1] for area/length; exactly 1 for presence
    - overlap_measure and feature_measure are in metres / m² (> 1),
      confirming ST_Area/ST_Length use the spheroid rather than degree units
    """
    app_path = tmp_path / "reimagina_aoi.duckdb"
    test_path = tmp_path / "reimagina_aoi_test.duckdb"

    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path),
        test_dsn=_duckdb_dsn(test_path),
    )

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)

        # Project stub required by the AOI FK
        project_id = str(uuid4())
        conn.execute(
            "INSERT INTO app_core.project (id, name) VALUES (?, ?)",
            [project_id, "aoi-test-project"],
        )

        source_id = cat.register_source(
            conn,
            name="aoi_test_source",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/aoi_test.geojson",
            version="v1",
        )
        run_id = cat.start_ingestion_run(
            conn,
            source_id=source_id,
            orchestrator="pytest",
            orchestrator_run_id=str(uuid4()),
            transformation_step="silver_to_curated",
            code_version="test",
        )

        # Polygon feature (building — geometry_family=polygon)
        poly_type_id = feat.get_type_id(conn, "global", "building")
        poly_gdf = gpd.GeoDataFrame(
            [{"id": "poly-1", "geometry": Polygon([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn,
            poly_gdf,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=poly_type_id,
            dataset_version="v1",
            source_record_ref_col="id",
        )

        # Line feature (road — geometry_family=line)
        line_type_id = feat.get_type_id(conn, "global", "road")
        line_gdf = gpd.GeoDataFrame(
            [{"id": "line-1", "geometry": LineString([(0.1, 1.1), (0.9, 1.1)])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn,
            line_gdf,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=line_type_id,
            dataset_version="v1",
            source_record_ref_col="id",
        )

        # Point feature (tree — geometry_family=point)
        point_type_id = feat.get_type_id(conn, "global", "tree")
        point_gdf = gpd.GeoDataFrame(
            [{"id": "point-1", "geometry": Point(0.5, 1.5)}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn,
            point_gdf,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=point_type_id,
            dataset_version="v1",
            source_record_ref_col="id",
        )

        # AOI that covers all three features
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name="test-aoi",
            geometry_wkt="POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))",
        )

        total = aoi_mod.compute_aoi_features(conn, aoi_id)
        assert total == 3, f"Expected 3 overlapping features, got {total}"

        rows = conn.execute(
            """
            SELECT CAST(overlap_basis AS VARCHAR)  AS overlap_basis,
                   overlap_measure,
                   feature_measure,
                   overlap_ratio
            FROM curated_geo.aoi_feature
            WHERE CAST(aoi_id AS VARCHAR) = ?
            ORDER BY overlap_basis
            """,
            [aoi_id],
        ).fetchall()

        by_basis = {r[0]: {"overlap_measure": r[1], "feature_measure": r[2], "overlap_ratio": r[3]} for r in rows}

        # Polygon (area)
        assert "area" in by_basis, "Missing area row for polygon feature"
        area = by_basis["area"]
        # Spheroid area for ~0.8° × 0.8° polygon is several thousand km²
        assert area["overlap_measure"] > 1.0, (
            f"overlap_measure={area['overlap_measure']} looks like degrees², not m²"
        )
        assert area["feature_measure"] > 1.0
        assert 0 < area["overlap_ratio"] <= 1.0

        # Line (length)
        assert "length" in by_basis, "Missing length row for line feature"
        length = by_basis["length"]
        # ~0.8° of longitude at equator ≈ 89 km = 89_000 m
        assert length["overlap_measure"] > 1.0, (
            f"overlap_measure={length['overlap_measure']} looks like degrees, not metres"
        )
        assert length["feature_measure"] > 1.0
        assert 0 < length["overlap_ratio"] <= 1.0

        # Point (presence)
        assert "presence" in by_basis, "Missing presence row for point feature"
        presence = by_basis["presence"]
        assert presence["overlap_ratio"] == 1.0
        assert presence["overlap_measure"] == 1.0
        assert presence["feature_measure"] == 1.0


def test_duckdb_serving_pagination(tmp_path):
    """get_features respects limit and returns a usable next_cursor.

    Insert 5 buildings, page through 2 at a time, verify:
    - each page has the right count
    - next_cursor is None on the final page
    - no features are duplicated or skipped across pages
    """
    app_path = tmp_path / "reimagina_page.duckdb"
    test_path = tmp_path / "reimagina_page_test.duckdb"

    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path),
        test_dsn=_duckdb_dsn(test_path),
    )

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        source_id = cat.register_source(
            conn,
            name="pagination_test",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/page_test.geojson",
            version="v1",
        )
        run_id = cat.start_ingestion_run(
            conn,
            source_id=source_id,
            orchestrator="pytest",
            orchestrator_run_id=str(uuid4()),
            transformation_step="silver_to_curated",
            code_version="test",
        )
        type_id = feat.get_type_id(conn, "global", "building")

        gdf = gpd.GeoDataFrame(
            [
                {"id": f"b-{i}", "geometry": Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)])}
                for i in range(5)
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn,
            gdf,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=type_id,
            dataset_version="v1",
            source_record_ref_col="id",
        )

        seen_ids: list[str] = []
        cursor = None
        pages = 0

        while True:
            page = serving.get_features(conn, type_slug="building", limit=2, after_cursor=cursor)
            batch = page["features"]
            assert len(batch) <= 2
            seen_ids.extend(row["id"] for row in batch)
            cursor = page["next_cursor"]
            pages += 1
            if cursor is None:
                break

        assert len(seen_ids) == 5, f"Expected 5 unique features, got {len(seen_ids)}"
        assert len(set(seen_ids)) == 5, "Duplicate features detected across pages"
        assert pages == 3, f"Expected 3 pages (2+2+1), got {pages}"


def test_duckdb_feature_overlap_polygon_contains_point(tmp_path):
    """compute_feature_overlaps inserts presence rows for points inside polygons.

    One polygon (building) fully contains one point (tree) and does not contain
    a second point outside its bounds.  Validates:
    - correct row count returned
    - overlap_basis = 'presence', overlap_ratio = 1.0
    - the outside point produces no row
    """
    app_path = tmp_path / "reimagina_overlap.duckdb"
    test_path = tmp_path / "reimagina_overlap_test.duckdb"

    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path),
        test_dsn=_duckdb_dsn(test_path),
    )

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        source_id = cat.register_source(
            conn,
            name="overlap_test_source",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/overlap_test.geojson",
            version="v1",
        )
        run_id = cat.start_ingestion_run(
            conn,
            source_id=source_id,
            orchestrator="pytest",
            orchestrator_run_id=str(uuid4()),
            transformation_step="silver_to_curated",
            code_version="test",
        )

        # Polygon feature
        poly_type_id = feat.get_type_id(conn, "global", "building")
        poly_gdf = gpd.GeoDataFrame(
            [{"id": "poly-1", "geometry": Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, poly_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=poly_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Point inside the polygon
        point_type_id = feat.get_type_id(conn, "global", "tree")
        point_gdf = gpd.GeoDataFrame(
            [
                {"id": "pt-inside",  "geometry": Point(1.0, 1.0)},
                {"id": "pt-outside", "geometry": Point(5.0, 5.0)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, point_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=point_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Compute overlaps: source = point (tree), target = polygon (building)
        count = ovlp.compute_feature_overlaps(conn, point_type_id, poly_type_id)
        assert count == 1, f"Expected 1 overlap row, got {count}"

        rows = conn.execute("""
            SELECT CAST(overlap_basis AS VARCHAR), overlap_ratio, overlap_measure, feature_measure
            FROM curated_geo.feature_overlap
        """).fetchall()

        assert len(rows) == 1
        basis, ratio, measure, feat_measure = rows[0]
        assert basis == "presence"
        assert ratio == 1.0
        assert measure == 1.0
        assert feat_measure == 1.0
