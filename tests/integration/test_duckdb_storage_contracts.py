from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from reimagina.data import aoi as aoi_mod
from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import observation as obs
from reimagina.data import overlap as ovlp
from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.jobs import overlap as overlap_jobs
from reimagina.jobs.aoi_summary import summarise_aoi
from reimagina.jobs.rollup import run_rollup
from reimagina.db import bootstrap_local_platform, connect, verify_required_relations
from reimagina.pipeline.loader import load_vector_source
from reimagina.storage import backend_for_connection
from reimagina.tiles.service import resolve_tile_sources


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

        page = backend_for_connection(conn).serving_reader().get_features(type_slug="building")
        rows = page["features"]
        assert page["next_cursor"] is None
        assert len(rows) == 1
        assert rows[0]["type_slug"] == "building"
        assert rows[0]["attributes"]["class"] == "residential"
        assert rows[0]["attributes"]["names"] == [{"primary": "Tower One"}]
        assert rows[0]["attributes"]["status"] == "active"

        layer_rows = backend_for_connection(conn).explore_reader().list_layers()
        assert any(row["logical_name"] == "serving_features" for row in layer_rows)

        feature_stats = backend_for_connection(conn).explore_reader().list_feature_stats()
        assert any(row["type_slug"] == "building" and row["row_count"] == 1 for row in feature_stats)

        serving_stats = backend_for_connection(conn).explore_reader().list_serving_stats()
        assert any(row["stat_scope"] == "all" and row["row_count"] == 1 for row in serving_stats)

        snapshot_id = backend_for_connection(conn).tile_repo().register_snapshot(
            layer_id="admin_reference",
            artifact_path="/tmp/admin_reference.pmtiles",
            artifact_uri="http://localhost:3001/admin_reference.pmtiles",
            snapshot_version="20260415T120000Z",
            source_relation="serving_geo.admin_v",
            source_lineage_ref={"latest_successful_ingestion_run_id": None},
        )
        assert snapshot_id

        sources = resolve_tile_sources(
            conn,
            live_tilejson_base_url="http://localhost:3000/get_tile",
            snapshot_base_url="http://localhost:3001",
        )
        by_id = {row["layer_id"]: row for row in sources}
        assert by_id["admin_reference"]["source_mode"] == "pmtiles_snapshot"
        assert by_id["admin_reference"]["available"] is True
        assert by_id["baseline_live"]["source_mode"] == "live_mvt"


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
            page = backend_for_connection(conn).serving_reader().get_features(type_slug="building", limit=2, after_cursor=cursor)
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


def test_duckdb_overlap_jobs_declare_and_compute_all_pairs(tmp_path):
    """jobs.overlap.declare_pair + compute_all_pairs orchestrate declared MEX pairs.

    Declares (MEX, block) → (MEX, lot) and verifies:
    - declare_pair is idempotent (second call returns the same ids, no extra rows)
    - list_pairs surfaces the declared pair joined with catalog slugs
    - compute_all_pairs returns per-pair counts and a total
    - a polygon-polygon overlap produces an 'area' row in metres (geodesic)
    - rerunning is idempotent (ON CONFLICT upsert)
    """
    app_path = tmp_path / "reimagina_overlap_jobs.duckdb"
    test_path = tmp_path / "reimagina_overlap_jobs_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        source_id = cat.register_source(
            conn,
            name="overlap_jobs_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/overlap_jobs.geojson",
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

        block_type_id = feat.get_type_id(conn, "MEX", "block")
        lot_type_id = feat.get_type_id(conn, "MEX", "lot")

        # One block, one lot that sits entirely inside the block.
        # Coordinates are near Mexico City (lon ~-99.13, lat ~19.43); small
        # degree deltas keep the geometry inside the block and give a
        # well-defined partial-area overlap on WGS84.
        block_gdf = gpd.GeoDataFrame(
            [{"id": "blk-1", "geometry": Polygon([
                (-99.140, 19.430), (-99.130, 19.430),
                (-99.130, 19.440), (-99.140, 19.440),
            ])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, block_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        lot_gdf = gpd.GeoDataFrame(
            [{"id": "lot-1", "geometry": Polygon([
                (-99.138, 19.432), (-99.134, 19.432),
                (-99.134, 19.436), (-99.138, 19.436),
            ])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, lot_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=lot_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        pair1 = overlap_jobs.declare_pair(
            conn,
            source_context="MEX", source_slug="lot",
            target_context="MEX", target_slug="block",
        )
        pair2 = overlap_jobs.declare_pair(
            conn,
            source_context="MEX", source_slug="lot",
            target_context="MEX", target_slug="block",
        )
        assert pair1 == pair2
        row_count = conn.execute(
            "SELECT COUNT(*) FROM curated_geo.feature_overlap_type_pair"
        ).fetchone()[0]
        assert row_count == 1

        pairs = overlap_jobs.list_pairs(conn)
        assert len(pairs) == 1
        assert pairs[0]["source"] == "MEX:lot"
        assert pairs[0]["target"] == "MEX:block"

        first = overlap_jobs.compute_all_pairs(conn)
        assert first["rows_total"] == 1
        assert first["pairs"][0]["rows"] == 1

        basis, measure, feat_measure, ratio = conn.execute("""
            SELECT CAST(overlap_basis AS VARCHAR), overlap_measure, feature_measure, overlap_ratio
            FROM curated_geo.feature_overlap
        """).fetchone()
        assert basis == "area"
        # The lot sits entirely inside the block, so the geodesic intersection
        # area equals the lot's own area and overlap_ratio is 1.0.
        assert ratio == 1.0
        assert measure == feat_measure
        # Sanity-check the geodesic measurement order of magnitude: a
        # 0.004° × 0.004° polygon at ~19.4° lat is roughly 1.8e5 m².
        assert 1.0e5 < measure < 3.0e5

        # Re-run: still one row (upsert), count returned is 1.
        second = overlap_jobs.compute_all_pairs(conn)
        assert second["rows_total"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM curated_geo.feature_overlap"
        ).fetchone()[0] == 1


def test_duckdb_overlap_compute_aoi_scoping(tmp_path):
    """aoi_id filters source features: lots outside the AOI produce no overlap rows."""
    app_path = tmp_path / "reimagina_overlap_aoi.duckdb"
    test_path = tmp_path / "reimagina_overlap_aoi_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        source_id = cat.register_source(
            conn,
            name="overlap_aoi_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/overlap_aoi.geojson",
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

        block_type_id = feat.get_type_id(conn, "MEX", "block")
        lot_type_id = feat.get_type_id(conn, "MEX", "lot")

        # Two non-overlapping blocks side by side.
        blocks = gpd.GeoDataFrame(
            [
                {"id": "blk-L", "geometry": Polygon([
                    (-99.150, 19.430), (-99.140, 19.430),
                    (-99.140, 19.440), (-99.150, 19.440),
                ])},
                {"id": "blk-R", "geometry": Polygon([
                    (-99.130, 19.430), (-99.120, 19.430),
                    (-99.120, 19.440), (-99.130, 19.440),
                ])},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, blocks,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # One lot inside each block.
        lots = gpd.GeoDataFrame(
            [
                {"id": "lot-L", "geometry": Polygon([
                    (-99.148, 19.432), (-99.142, 19.432),
                    (-99.142, 19.438), (-99.148, 19.438),
                ])},
                {"id": "lot-R", "geometry": Polygon([
                    (-99.128, 19.432), (-99.122, 19.432),
                    (-99.122, 19.438), (-99.128, 19.438),
                ])},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, lots,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=lot_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        overlap_jobs.declare_pair(
            conn,
            source_context="MEX", source_slug="lot",
            target_context="MEX", target_slug="block",
        )

        # AOI that covers only the LEFT block.
        project_id = str(uuid4())
        conn.execute(
            "INSERT INTO app_core.project (id, name) VALUES (?, ?)",
            [project_id, "aoi-scope-test"],
        )
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name="left-aoi",
            geometry_wkt=(
                "POLYGON ((-99.150 19.430, -99.140 19.430,"
                " -99.140 19.440, -99.150 19.440, -99.150 19.430))"
            ),
        )

        scoped = overlap_jobs.compute_all_pairs(conn, aoi_id=aoi_id)
        assert scoped["rows_total"] == 1

        (source_ref,) = conn.execute("""
            SELECT f.source_record_ref
            FROM curated_geo.feature_overlap fo
            JOIN curated_geo.feature f ON f.id = fo.source_feature_id
        """).fetchone()
        assert source_ref == "lot-L"

        # Unscoped run should find both pairs.
        unscoped = overlap_jobs.compute_all_pairs(conn)
        assert unscoped["rows_total"] == 2


def test_duckdb_aoi_summary_upsert_list_and_serving_view(tmp_path):
    """AOISummaryRepository upserts are idempotent; serving_geo.aoi_summary_v
    returns only status='current' rows.
    """
    app_path = tmp_path / "reimagina_aoi_summary.duckdb"
    test_path = tmp_path / "reimagina_aoi_summary_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        project_id = str(uuid4())
        conn.execute(
            "INSERT INTO app_core.project (id, name) VALUES (?, ?)",
            [project_id, "aoi-summary-test"],
        )
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name="summary-aoi",
            geometry_wkt="POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))",
        )

        summary_repo = backend_for_connection(conn).aoi_summary_repo()
        indicator_id = str(uuid4())

        first_id = summary_repo.upsert_summary(
            aoi_id=aoi_id, indicator_id=indicator_id, value=1.0,
            aggregation_note="seed",
        )
        # Re-upsert without scenario → same row id, updated value + note.
        second_id = summary_repo.upsert_summary(
            aoi_id=aoi_id, indicator_id=indicator_id, value=2.0,
            aggregation_note="reran",
        )
        assert first_id == second_id

        current = summary_repo.get_summary(aoi_id=aoi_id, indicator_id=indicator_id)
        assert current is not None
        assert current["value"] == 2.0
        assert current["aggregation_note"] == "reran"
        assert current["status"] == "current"

        # Scenario-scoped row is independent.
        scenario_id = str(uuid4())
        scenario_summary_id = summary_repo.upsert_summary(
            aoi_id=aoi_id, indicator_id=indicator_id, value=5.0,
            scenario_id=scenario_id,
        )
        assert scenario_summary_id != first_id

        # list_summaries filters by scenario scope and status.
        base_rows = summary_repo.list_summaries(aoi_id=aoi_id)
        assert [r["value"] for r in base_rows] == [2.0]

        scenario_rows = summary_repo.list_summaries(aoi_id=aoi_id, scenario_id=scenario_id)
        assert [r["value"] for r in scenario_rows] == [5.0]

        # Stale rows fall out of the serving view + default list.
        summary_repo.upsert_summary(
            aoi_id=aoi_id, indicator_id=indicator_id, value=2.0,
            status="stale",
        )
        assert summary_repo.list_summaries(aoi_id=aoi_id) == []
        assert summary_repo.list_summaries(aoi_id=aoi_id, only_current=False) != []

        serving_rows = conn.execute(
            """
            SELECT CAST(indicator_id AS VARCHAR), value
            FROM serving_geo.aoi_summary_v
            WHERE aoi_id = ?
            """,
            [aoi_id],
        ).fetchall()
        # Only the scenario-scoped row remains 'current'.
        assert serving_rows == [(indicator_id, 5.0)]

        # New explore layer rows show up.
        layers = conn.execute(
            "SELECT schema_name, object_name FROM explore.layer_catalog_v"
        ).fetchall()
        names = {(row[0], row[1]) for row in layers}
        assert ("curated_geo", "aoi_indicator_summary") in names
        assert ("serving_geo", "aoi_summary_v") in names


def test_duckdb_block_compactness_end_to_end(tmp_path):
    """Seed catalog → load 3 blocks of known compactness → compute → assert.

    Exercises the full vertical slice: catalog upsert, job_run lifecycle,
    bulk indicator_result write through the Python validator, and the
    serving_geo.current_indicator_v read contract.
    """
    app_path = tmp_path / "reimagina_l2.duckdb"
    test_path = tmp_path / "reimagina_l2_test.duckdb"
    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path)
    )

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        seed_catalog(conn)

        source_id = cat.register_source(
            conn,
            name="block_compactness_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/compactness.geojson",
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

        block_type_id = feat.get_type_id(conn, "MEX", "block")
        # Three shapes of known compactness at ~19.4° lat. Using a ~0.004°
        # square gives ~440 m sides so planar-UTM metres are order-of-magnitude
        # right; exact values are asserted via polsby_popper(...).
        square = [
            (-99.140, 19.430), (-99.136, 19.430),
            (-99.136, 19.434), (-99.140, 19.434),
        ]
        thin_rect = [
            (-99.130, 19.430), (-99.126, 19.430),
            (-99.126, 19.4301), (-99.130, 19.4301),
        ]
        near_circle = []
        import math as _m
        cx, cy, r = -99.120, 19.432, 0.002
        for i in range(32):
            theta = 2 * _m.pi * i / 32
            near_circle.append((cx + r * _m.cos(theta), cy + r * _m.sin(theta)))

        blocks = gpd.GeoDataFrame(
            [
                {"id": "sq",  "geometry": Polygon(square)},
                {"id": "rect", "geometry": Polygon(thin_rect)},
                {"id": "circle", "geometry": Polygon(near_circle)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn,
            blocks,
            source_id=source_id,
            ingestion_run_id=run_id,
            type_id=block_type_id,
            dataset_version="v1",
            source_record_ref_col="id",
        )

        result = compute_block_compactness(conn, code_version="test")
        assert result.rows_written == 3
        assert result.features_considered == 3

        # Read back through the serving contract.
        result_repo = backend_for_connection(conn).indicator_result_repo()
        current_rows = result_repo.list_current_for_indicator(
            indicator_slug="urban_form_block_compactness"
        )
        assert len(current_rows) == 3
        by_feature: dict[str, float] = {}
        for row in current_rows:
            ref = conn.execute(
                "SELECT source_record_ref FROM curated_geo.feature WHERE CAST(id AS VARCHAR) = ?",
                [row["feature_id"]],
            ).fetchone()[0]
            by_feature[ref] = row["value_numeric"]
            assert row["quality_flag"] == "measured"
            assert row["indicator_slug"] == "urban_form_block_compactness"
            assert row["value_type"] == "numeric"
            assert row["unit"] == "ratio_0_1"

        # Assertions on the math. Tolerances generous because the
        # projection isn't perfectly metric at this distance from the UTM
        # central meridian.
        assert 0.0 <= by_feature["rect"] < 0.1, by_feature
        assert 0.7 < by_feature["sq"] < 0.9, by_feature
        assert by_feature["circle"] > 0.95, by_feature

        # Re-running the worker must be idempotent: same row count, and the
        # old 'current' rows become 'stale'.
        second = compute_block_compactness(conn, code_version="test")
        assert second.rows_written == 3
        current_after = result_repo.list_current_for_indicator(
            indicator_slug="urban_form_block_compactness"
        )
        assert len(current_after) == 3  # still three current rows
        stale_count = conn.execute(
            """
            SELECT COUNT(*) FROM curated_geo.indicator_result
            WHERE indicator_slug = 'urban_form_block_compactness'
              AND status = 'stale'
            """
        ).fetchone()[0]
        assert stale_count == 3  # the previous current rows

        # Budget-keeping: validator rejects out-of-range value_numeric.
        job_repo = backend_for_connection(conn).job_run_repo()
        synthetic_job = job_repo.start_job(
            model_family="urban_form", code_version="test", status="running"
        )
        feature_row = conn.execute(
            "SELECT CAST(id AS VARCHAR) FROM curated_geo.feature WHERE source_record_ref = 'sq'"
        ).fetchone()
        with pytest.raises(ValueError, match="above valid_max"):
            result_repo.write_result(
                feature_id=feature_row[0],
                indicator_slug="urban_form_block_compactness",
                job_run_id=synthetic_job,
                value_numeric=2.0,
            )


def test_duckdb_rollup_and_aoi_summary_end_to_end(tmp_path):
    """block→ageb rollup + aoi_indicator_summary on DuckDB.

    Builds a miniature two-AGEB city: four blocks with known compactness
    values, two per AGEB, all inside one AOI. Exercises:
    - the overlap declare + compute path (block→ageb)
    - run_rollup writing quality_flag='estimated' AGEB rows
    - summarise_aoi producing one row in serving_geo.aoi_summary_v
    - missing-threshold behaviour (all-missing child set → 'missing' parent).
    """
    import math
    app_path = tmp_path / "reimagina_l2_rollup.duckdb"
    test_path = tmp_path / "reimagina_l2_rollup_test.duckdb"
    bootstrap_local_platform(
        app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path)
    )

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        seed_catalog(conn)

        source_id = cat.register_source(
            conn,
            name="rollup_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/rollup.geojson",
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

        block_type_id = feat.get_type_id(conn, "MEX", "block")
        ageb_type_id = feat.get_type_id(conn, "MEX", "ageb")

        def square(lon0: float, lat0: float, side: float) -> Polygon:
            return Polygon([
                (lon0, lat0),
                (lon0 + side, lat0),
                (lon0 + side, lat0 + side),
                (lon0, lat0 + side),
            ])

        # Two AGEB polygons side by side. Each covers two blocks cleanly.
        ageb_gdf = gpd.GeoDataFrame(
            [
                {"id": "ageb-L", "geometry": square(-99.140, 19.430, 0.008)},
                {"id": "ageb-R", "geometry": square(-99.130, 19.430, 0.008)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, ageb_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=ageb_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Four blocks, two inside each AGEB. The near-circle block is much
        # more compact than the thin-rectangle block so we expect the
        # weighted-mean AGEB value to land between their two compactness
        # scores.
        near_circle_pts = []
        cx, cy, r = -99.136, 19.434, 0.0015
        for i in range(32):
            theta = 2 * math.pi * i / 32
            near_circle_pts.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))

        block_gdf = gpd.GeoDataFrame(
            [
                {"id": "blk-L-sq", "geometry": square(-99.139, 19.431, 0.002)},
                {"id": "blk-L-rect", "geometry": Polygon([
                    (-99.137, 19.431), (-99.133, 19.431),
                    (-99.133, 19.4311), (-99.137, 19.4311),
                ])},
                {"id": "blk-R-sq",    "geometry": square(-99.129, 19.431, 0.002)},
                {"id": "blk-R-circle", "geometry": Polygon(near_circle_pts)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, block_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Shift the circle to inside ageb-R so it actually overlaps.
        conn.execute(
            """
            UPDATE curated_geo.feature
            SET geometry = ST_GeomFromText(?)
            WHERE source_record_ref = 'blk-R-circle'
            """,
            [Polygon([
                (-99.126 + 0.0015 * math.cos(2*math.pi*i/32),
                 19.434  + 0.0015 * math.sin(2*math.pi*i/32))
                for i in range(32)
            ]).wkt],
        )

        # Compute compactness (block level).
        compute_block_compactness(conn, code_version="test")

        # Declare + compute the block→ageb overlap.
        overlap_jobs.declare_pair(
            conn,
            source_context="MEX", source_slug="block",
            target_context="MEX", target_slug="ageb",
        )
        overlap_jobs.compute_all_pairs(conn)

        # Run the rollup.
        rollup = run_rollup(
            conn,
            indicator_slug="urban_form_block_compactness",
            source_feature_type="block",
            target_feature_type="ageb",
            code_version="test",
        )
        assert rollup.targets_considered == 2
        assert rollup.rows_written == 2
        assert rollup.aggregation_method == "weighted_mean"

        # Parent-grain rows show up through the serving view with
        # quality_flag='estimated'.
        parents = conn.execute(
            """
            SELECT f.source_record_ref, ir.value_numeric, ir.quality_flag
            FROM curated_geo.indicator_result ir
            JOIN curated_geo.feature f ON f.id = ir.feature_id
            WHERE ir.indicator_slug = 'urban_form_block_compactness'
              AND ir.status = 'current' AND ir.feature_type = 'ageb'
            ORDER BY f.source_record_ref
            """
        ).fetchall()
        assert len(parents) == 2
        for _ref, value, flag in parents:
            assert flag == "estimated"
            assert 0.0 < value <= 1.0

        # Build an AOI that covers both AGEBs, then summarise.
        project_id = str(uuid4())
        conn.execute(
            "INSERT INTO app_core.project (id, name) VALUES (?, ?)",
            [project_id, "rollup-test"],
        )
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name="rollup-aoi",
            geometry_wkt="POLYGON((-99.142 19.428,-99.118 19.428,-99.118 19.440,-99.142 19.440,-99.142 19.428))",
        )
        aoi_mod.compute_aoi_features(conn, aoi_id)

        summary = summarise_aoi(
            conn,
            aoi_id=aoi_id,
            indicator_slug="urban_form_block_compactness",
        )
        assert summary.status == "current"
        assert summary.value is not None
        assert 0.0 < summary.value <= 1.0

        serving_rows = conn.execute(
            """
            SELECT value FROM serving_geo.aoi_summary_v
            WHERE aoi_id = ?
            """,
            [aoi_id],
        ).fetchall()
        assert len(serving_rows) == 1
        assert serving_rows[0][0] == pytest.approx(summary.value)


def test_duckdb_rollup_weighted_mean_is_area_weighted(tmp_path):
    """Rollup weights weighted_mean by feature_overlap.overlap_measure.

    Builds a tiny AGEB with two blocks of **very different sizes** and
    **different indicator values**. Under plain arithmetic mean the AGEB
    value would be the midpoint of the two child values; under area-
    weighted mean (correct) it must be pulled toward the larger block's
    value. Asserting the latter catches any regression that reverts to
    overlap_ratio weighting.
    """
    import math
    app_path = tmp_path / "reimagina_rollup_weight.duckdb"
    test_path = tmp_path / "reimagina_rollup_weight_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        seed_catalog(conn)

        source_id = cat.register_source(
            conn,
            name="rollup_weight_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/rollup_weight.geojson",
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
        block_type_id = feat.get_type_id(conn, "MEX", "block")
        ageb_type_id = feat.get_type_id(conn, "MEX", "ageb")

        # AGEB covering both blocks.
        ageb_gdf = gpd.GeoDataFrame(
            [{"id": "big-ageb", "geometry": Polygon([
                (-99.140, 19.430), (-99.120, 19.430),
                (-99.120, 19.440), (-99.140, 19.440),
            ])}],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, ageb_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=ageb_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Tiny block (high compactness, circle-ish) + large block (low
        # compactness, thin rectangle). We'll confirm the weighted mean
        # gets pulled toward the large block's value (≈ 0 compactness).
        tiny_circle_pts = [
            (-99.138 + 0.0005 * math.cos(2 * math.pi * i / 32),
             19.432  + 0.0005 * math.sin(2 * math.pi * i / 32))
            for i in range(32)
        ]
        large_rect = Polygon([
            (-99.135, 19.431), (-99.122, 19.431),
            (-99.122, 19.439), (-99.135, 19.439),
        ])
        blocks_gdf = gpd.GeoDataFrame(
            [
                {"id": "tiny-circle", "geometry": Polygon(tiny_circle_pts)},
                {"id": "large-rect",   "geometry": large_rect},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, blocks_gdf,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        compute_block_compactness(conn, code_version="test")
        overlap_jobs.declare_pair(
            conn,
            source_context="MEX", source_slug="block",
            target_context="MEX", target_slug="ageb",
        )
        overlap_jobs.compute_all_pairs(conn)
        run_rollup(
            conn,
            indicator_slug="urban_form_block_compactness",
            source_feature_type="block",
            target_feature_type="ageb",
            code_version="test",
        )

        tiny_value, large_value = (row[0] for row in conn.execute(
            """
            SELECT ir.value_numeric
            FROM curated_geo.indicator_result ir
            JOIN curated_geo.feature f ON f.id = ir.feature_id
            WHERE ir.indicator_slug = 'urban_form_block_compactness'
              AND ir.feature_type = 'block' AND ir.status = 'current'
            ORDER BY CASE f.source_record_ref WHEN 'tiny-circle' THEN 0 ELSE 1 END
            """
        ).fetchall())

        ageb_value = conn.execute(
            """
            SELECT ir.value_numeric
            FROM curated_geo.indicator_result ir
            JOIN curated_geo.feature f ON f.id = ir.feature_id
            WHERE ir.indicator_slug = 'urban_form_block_compactness'
              AND ir.feature_type = 'ageb' AND ir.status = 'current'
            """
        ).fetchone()[0]

        # Arithmetic mean would sit at (tiny+large)/2. Area-weighted mean
        # must be far closer to `large_value` because the rectangle dwarfs
        # the circle in area. Assertion: distance to large-value is less
        # than half the distance to the arithmetic midpoint.
        arithmetic_mean = (tiny_value + large_value) / 2
        distance_to_large = abs(ageb_value - large_value)
        distance_to_midpoint = abs(ageb_value - arithmetic_mean)
        assert distance_to_large < distance_to_midpoint, (
            f"AGEB rollup {ageb_value:.4f} is closer to arithmetic mean "
            f"{arithmetic_mean:.4f} than to the large-block value "
            f"{large_value:.4f} — weighted_mean appears to use overlap_ratio."
        )


def test_duckdb_block_building_density_end_to_end(tmp_path):
    """Seed catalog + load 3 blocks + 6 buildings → compute density → assert.

    Covers the three real outcomes:
    - dense block (3 buildings centroid-inside) → ~30/ha
    - sparse block (1 building) → ~10/ha
    - empty block (no buildings) → explicit 0.0/ha row with quality='measured'
      (NOT missing — 'no buildings observed here' is real signal).
    Also verifies buildings whose centroids fall *outside* any block are not
    double-counted or silently added to the nearest one.
    """
    app_path = tmp_path / "reimagina_density.duckdb"
    test_path = tmp_path / "reimagina_density_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        seed_catalog(conn)

        source_id = cat.register_source(
            conn,
            name="density_src",
            provider="pytest",
            storage_type="vector",
            integration_role="canonical_entity",
            storage_uri="file:///tmp/density.geojson",
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
        block_type_id = feat.get_type_id(conn, "MEX", "block")
        building_type_id = feat.get_type_id(conn, "global", "building")

        # Three blocks around Mexico City latitude 19.43°.
        # Each ~0.003° × 0.003° ≈ 330m × 314m ≈ 10.4 ha.
        def square(lon0: float, lat0: float) -> Polygon:
            return Polygon([
                (lon0, lat0),
                (lon0 + 0.003, lat0),
                (lon0 + 0.003, lat0 + 0.003),
                (lon0, lat0 + 0.003),
            ])

        blocks = gpd.GeoDataFrame(
            [
                {"id": "dense",  "geometry": square(-99.140, 19.430)},
                {"id": "sparse", "geometry": square(-99.130, 19.430)},
                {"id": "empty",  "geometry": square(-99.120, 19.430)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, blocks,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        # Tiny building footprints so `ST_Within(ST_Centroid(...))` clearly
        # picks one block even under numerical jitter. Three in `dense`,
        # one in `sparse`, zero in `empty`, one *outside all three* to prove
        # the attribution rule rejects it.
        def bldg(lon: float, lat: float) -> Polygon:
            return Polygon([
                (lon, lat),
                (lon + 0.0001, lat),
                (lon + 0.0001, lat + 0.0001),
                (lon, lat + 0.0001),
            ])

        buildings = gpd.GeoDataFrame(
            [
                {"id": "bd-1", "geometry": bldg(-99.139, 19.431)},
                {"id": "bd-2", "geometry": bldg(-99.138, 19.432)},
                {"id": "bd-3", "geometry": bldg(-99.1375, 19.4315)},
                {"id": "bs-1", "geometry": bldg(-99.129, 19.431)},
                {"id": "b-outside", "geometry": bldg(-99.100, 19.400)},
            ],
            crs="EPSG:4326",
        )
        feat.load_features(
            conn, buildings,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=building_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )

        result = compute_block_building_density(conn, code_version="test")
        assert result.rows_written == 3
        assert result.blocks_considered == 3
        # bd-1, bd-2, bd-3 → dense; bs-1 → sparse; b-outside → no block.
        assert result.buildings_attributed == 4

        result_repo = backend_for_connection(conn).indicator_result_repo()
        current = result_repo.list_current_for_indicator(
            indicator_slug="urban_form_block_building_density"
        )
        by_ref: dict[str, float] = {}
        qualities: dict[str, str] = {}
        for row in current:
            ref = conn.execute(
                "SELECT source_record_ref FROM curated_geo.feature WHERE CAST(id AS VARCHAR) = ?",
                [row["feature_id"]],
            ).fetchone()[0]
            by_ref[ref] = row["value_numeric"]
            qualities[ref] = row["quality_flag"]

        # Area ~10.4 ha at this latitude; 3 buildings / 10.4 ha ≈ 0.29/ha.
        # Assert ratios and the zero case, not exact magnitudes.
        assert by_ref["empty"] == 0.0
        assert qualities["empty"] == "measured"
        assert by_ref["sparse"] > 0.0
        assert by_ref["dense"] == pytest.approx(3 * by_ref["sparse"], rel=1e-6)

        # valid_max enforcement: writing a 10000/ha value must trip the
        # Python-side validator (mirror of the Postgres trigger).
        job_repo = backend_for_connection(conn).job_run_repo()
        synthetic_job = job_repo.start_job(
            model_family="urban_form", code_version="test", status="running"
        )
        (dense_id,) = conn.execute(
            "SELECT CAST(id AS VARCHAR) FROM curated_geo.feature WHERE source_record_ref = 'dense'"
        ).fetchone()
        with pytest.raises(ValueError, match="above valid_max"):
            result_repo.write_result(
                feature_id=dense_id,
                indicator_slug="urban_form_block_building_density",
                job_run_id=synthetic_job,
                value_numeric=10_000.0,
            )


def test_duckdb_list_current_with_geometry_round_trips_wkb(tmp_path):
    """list_current_with_geometry returns parseable EPSG:4326 WKB + the value.

    Backs the API's GeoJSON + GeoParquet download paths. Asserts the WKB
    is non-empty bytes that shapely can parse and that the polygon it
    decodes to is the same geometry we loaded.
    """
    import shapely.wkb
    app_path = tmp_path / "reimagina_wkb.duckdb"
    test_path = tmp_path / "reimagina_wkb_test.duckdb"
    bootstrap_local_platform(app_dsn=_duckdb_dsn(app_path), test_dsn=_duckdb_dsn(test_path))

    with connect(_duckdb_dsn(app_path)) as conn:
        feat.seed_feature_types(conn)
        seed_catalog(conn)

        source_id = cat.register_source(
            conn,
            name="wkb_src", provider="pytest",
            storage_type="vector", integration_role="canonical_entity",
            storage_uri="file:///tmp/wkb.geojson", version="v1",
        )
        run_id = cat.start_ingestion_run(
            conn, source_id=source_id,
            orchestrator="pytest", orchestrator_run_id=str(uuid4()),
            transformation_step="silver_to_curated", code_version="test",
        )
        block_type_id = feat.get_type_id(conn, "MEX", "block")

        block_polygon = Polygon([
            (-99.140, 19.430), (-99.137, 19.430),
            (-99.137, 19.433), (-99.140, 19.433),
        ])
        blocks = gpd.GeoDataFrame(
            [{"id": "blk-1", "geometry": block_polygon}], crs="EPSG:4326"
        )
        feat.load_features(
            conn, blocks,
            source_id=source_id, ingestion_run_id=run_id,
            type_id=block_type_id, dataset_version="v1",
            source_record_ref_col="id",
        )
        compute_block_compactness(conn, code_version="test")

        rows = backend_for_connection(conn).indicator_result_repo().list_current_with_geometry(
            indicator_slug="urban_form_block_compactness",
            feature_type="block",
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["type_slug"] == "block"
        assert row["value_numeric"] is not None
        assert isinstance(row["geometry_wkb"], (bytes, bytearray))
        assert len(row["geometry_wkb"]) > 0
        decoded = shapely.wkb.loads(bytes(row["geometry_wkb"]))
        # Exact-equality via shapely's equals (same vertex order + coords).
        assert decoded.equals(block_polygon), (decoded, block_polygon)
