from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import aoi
from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import file_asset
from reimagina.data import observation as obs
from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.jobs import overlap as overlap_jobs
from reimagina.jobs.aoi_summary import summarise_aoi
from reimagina.pipeline import loader
from reimagina.storage import backend_for_connection
from reimagina.tiles.service import export_pmtiles_snapshot, resolve_tile_sources
from tests.integration._helpers import (
    insert_feature as _insert_feature,
    insert_project as _insert_project,
    register_source as _register_source,
    start_run as _start_run,
)


def test_failed_vector_load_persists_failed_ingestion_run(conn):
    source_id = _register_source(
        conn,
        name="broken_vector",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    type_id = feat.get_type_id(conn, "MEX", "block")

    with pytest.raises(Exception):
        loader.load_vector_source(
            conn,
            source_id=source_id,
            silver_path=Path("/tmp/does-not-exist.geojson"),
            type_id=type_id,
            dataset_version="v1",
            source_record_ref_col="id",
            orchestrator_run_id=str(uuid4()),
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, details
            FROM curated_geo.ingestion_run
            WHERE source_id = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (source_id,),
        )
        status, details = cur.fetchone()

    assert status == "failed"
    assert details["error_type"]
    assert details["error_message"]


def test_source_registration_is_idempotent_per_provider_name_version(conn):
    first = cat.register_source(
        conn,
        name="Overture Buildings - Mexico City BBox",
        provider="Overture Maps Foundation",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file://data/bronze/first.geojson",
        version="2026-02-18.0",
        notes="first registration",
    )
    second = cat.register_source(
        conn,
        name="Overture Buildings - Mexico City BBox",
        provider="Overture Maps Foundation",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file://data/bronze/second.geojson",
        version="2026-02-18.0",
        notes="updated registration",
    )

    assert first == second

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), storage_uri
            FROM curated_geo.data_source_catalog
            WHERE name = %s AND provider = %s AND version = %s
            GROUP BY storage_uri
            """,
            (
                "Overture Buildings - Mexico City BBox",
                "Overture Maps Foundation",
                "2026-02-18.0",
            ),
        )
        count, storage_uri = cur.fetchone()

    assert count == 1
    assert storage_uri == "file://data/bronze/second.geojson"


def test_explore_views_surface_source_and_feature_counts(conn):
    source_id = _register_source(
        conn,
        name="explore_source",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id, step="silver_to_curated")
    type_id = feat.get_type_id(conn, "global", "building")
    _insert_feature(
        conn,
        type_id=type_id,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="building-1",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )

    layers = backend_for_connection(conn).explore_reader().list_layers()
    assert any(row["logical_name"] == "feature_type_stats" for row in layers)

    sources = backend_for_connection(conn).explore_reader().list_sources()
    assert any(
        row["source_id"] == source_id and row["ingestion_run_id"] == run_id
        for row in sources
    )

    feature_stats = backend_for_connection(conn).explore_reader().list_feature_stats()
    assert any(
        row["source_id"] == source_id
        and row["type_slug"] == "building"
        and row["row_count"] == 1
        for row in feature_stats
    )

    serving_stats = backend_for_connection(conn).explore_reader().list_serving_stats()
    assert any(
        row["stat_scope"] == "all" and row["row_count"] >= 1 for row in serving_stats
    )


def test_tile_snapshot_registry_and_source_resolution(conn):
    latest = (
        backend_for_connection(conn)
        .tile_repo()
        .get_latest_snapshot(layer_id="admin_reference")
    )
    assert latest is None

    snapshot_id = (
        backend_for_connection(conn)
        .tile_repo()
        .register_snapshot(
            layer_id="admin_reference",
            artifact_path="/tmp/admin_reference-20260415T120000Z.pmtiles",
            artifact_uri="http://localhost:3001/admin_reference-20260415T120000Z.pmtiles",
            snapshot_version="20260415T120000Z",
            source_relation="serving_geo.admin_v",
            source_lineage_ref={"latest_successful_ingestion_run_id": None},
        )
    )
    assert snapshot_id

    latest = (
        backend_for_connection(conn)
        .tile_repo()
        .get_latest_snapshot(layer_id="admin_reference")
    )
    assert latest is not None
    assert latest["layer_id"] == "admin_reference"

    sources = resolve_tile_sources(
        conn,
        live_tilejson_base_url="http://localhost:3000/get_tile",
        snapshot_base_url="http://localhost:3001",
    )
    by_id = {row["layer_id"]: row for row in sources}
    assert by_id["admin_reference"]["source_mode"] == "pmtiles_snapshot"
    assert by_id["admin_reference"]["available"] is True
    assert by_id["admin_reference"]["source_config"]["url"].endswith(".pmtiles")
    assert by_id["baseline_live"]["source_mode"] == "live_mvt"
    assert (
        by_id["baseline_live"]["source_config"]["tilejson_url"]
        == "http://localhost:3000/get_tile"
    )


def test_pmtiles_export_registers_snapshot_metadata(conn, monkeypatch, tmp_path):
    output_dir = tmp_path / "pmtiles"

    def fake_run_export(command: list[str]) -> None:
        Path(command[3]).write_text("pmtiles-placeholder")

    @contextmanager
    def fake_connect(_dsn: str):
        yield conn

    monkeypatch.setattr("reimagina.tiles.service._run_export_command", fake_run_export)
    monkeypatch.setattr("reimagina.tiles.service.managed_connection", fake_connect)

    payload = export_pmtiles_snapshot(
        dsn="postgresql://localhost/reimagina_test",
        layer_id="admin_reference",
        output_dir=output_dir,
        snapshot_base_url="http://localhost:3001",
        snapshot_version="20260415T130000Z",
    )

    assert payload["layer_id"] == "admin_reference"
    assert Path(payload["artifact_path"]).exists()

    latest = (
        backend_for_connection(conn)
        .tile_repo()
        .get_latest_snapshot(layer_id="admin_reference")
    )
    assert latest is not None
    assert latest["snapshot_version"] == "20260415T130000Z"
    assert (
        latest["artifact_uri"]
        == "http://localhost:3001/admin_reference-20260415T130000Z.pmtiles"
    )


def test_postgres_index_inventory_covers_lineage_and_fk_paths(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'curated_geo'
            """
        )
        index_names = {row[0] for row in cur.fetchall()}

    assert "feature_ingestion_run_idx" in index_names
    assert "feature_type_id_idx" in index_names
    assert "observation_ingestion_run_idx" in index_names
    assert "file_asset_ingestion_run_idx" in index_names
    assert "area_of_interest_project_idx" in index_names
    assert "ingestion_run_source_started_idx" in index_names


def test_aoi_overlap_ratio_is_clamped_to_one(conn):
    source_id = _register_source(
        conn,
        name="aoi_ratio_source",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="block-ratio-1",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )
    project_id = _insert_project(conn, name="AOI Ratio Project")
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="Exact Match AOI",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )

    inserted = aoi.compute_aoi_features(conn, aoi_id)

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT overlap_ratio
            FROM curated_geo.aoi_feature
            WHERE aoi_id = %s AND feature_id = %s
            """,
            (aoi_id, block_id),
        )
        overlap_ratio = float(cur.fetchone()[0])

    assert overlap_ratio <= 1.0


def test_aoi_overlap_handles_invalid_feature_geometry(conn):
    source_id = _register_source(
        conn,
        name="aoi_invalid_geom_source",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="block-invalid-geom-1",
        geometry_wkt="POLYGON((0 0,2 2,2 0,0 2,0 0))",
    )
    project_id = _insert_project(conn, name="AOI Invalid Geometry Project")
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="Invalid Geometry AOI",
        geometry_wkt="POLYGON((0 0,2 0,2 2,0 2,0 0))",
    )

    inserted = aoi.compute_aoi_features(conn, aoi_id)

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT overlap_basis, overlap_ratio
            FROM curated_geo.aoi_feature
            WHERE aoi_id = %s AND feature_id = %s
            """,
            (aoi_id, block_id),
        )
        overlap_basis, overlap_ratio = cur.fetchone()

    assert overlap_basis == "area"
    assert 0 < float(overlap_ratio) <= 1.0


def test_feature_rejects_self_parenting(conn):
    source_id = _register_source(
        conn,
        name="hierarchy_source_self",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    type_id = feat.get_type_id(conn, "MEX", "block")
    feature_id = str(uuid4())

    with pytest.raises(psycopg2.Error):
        _insert_feature(
            conn,
            feature_id=feature_id,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref="self-parent",
            geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            parent_id=feature_id,
        )


def test_feature_rejects_hierarchy_cycle(conn):
    source_id = _register_source(
        conn,
        name="hierarchy_source_cycle",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    parent_type = feat.get_type_id(conn, "MEX", "municipality")
    child_type = feat.get_type_id(conn, "MEX", "block")

    parent_id = _insert_feature(
        conn,
        type_id=parent_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="parent",
        geometry_wkt="POLYGON((0 0,2 0,2 2,0 2,0 0))",
    )
    child_id = _insert_feature(
        conn,
        type_id=child_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="child",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
        parent_id=parent_id,
    )

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE curated_geo.feature SET parent_id = %s WHERE id = %s",
                (child_id, parent_id),
            )


def test_feature_rejects_parent_for_non_hierarchical_type(conn):
    source_id = _register_source(
        conn,
        name="hierarchy_source_tree",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    tree_type = feat.get_type_id(conn, "global", "tree")

    parent_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="block-parent",
        geometry_wkt="POLYGON((0 0,2 0,2 2,0 2,0 0))",
    )

    with pytest.raises(psycopg2.Error):
        _insert_feature(
            conn,
            type_id=tree_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref="tree-child",
            geometry_wkt="POINT(0.5 0.5)",
            parent_id=parent_id,
        )


def test_file_asset_rejects_source_run_mismatch(conn):
    asset_source_id = _register_source(
        conn,
        name="asset_source_a",
        storage_type="raster",
        integration_role="enrichment",
    )
    wrong_source_id = _register_source(
        conn,
        name="asset_source_b",
        storage_type="raster",
        integration_role="enrichment",
    )
    run_id = _start_run(conn, asset_source_id)

    with pytest.raises(psycopg2.Error):
        file_asset.register_file_asset(
            conn,
            source_id=wrong_source_id,
            ingestion_run_id=run_id,
            storage_uri="s3://bucket/ndvi.tif",
            dataset_version="v1",
            format="COG",
        )


def test_observation_refresh_keeps_single_current_row(conn):
    feature_source_id = _register_source(
        conn,
        name="feature_source_obs",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    feature_run_id = _start_run(conn, feature_source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    feature_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=feature_source_id,
        ingestion_run_id=feature_run_id,
        dataset_version="v1",
        source_record_ref="block-obs",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )

    obs_source_id = _register_source(
        conn,
        name="obs_source",
        storage_type="tabular",
        integration_role="enrichment",
    )
    run_1 = _start_run(conn, obs_source_id, step="obs_1")
    obs.upsert_observation(
        conn,
        feature_id=feature_id,
        attribute_key="population_total",
        value_type="numeric",
        value=100,
        source_id=obs_source_id,
        ingestion_run_id=run_1,
    )

    run_2 = _start_run(conn, obs_source_id, step="obs_2")
    obs.upsert_observation(
        conn,
        feature_id=feature_id,
        attribute_key="population_total",
        value_type="numeric",
        value=120,
        source_id=obs_source_id,
        ingestion_run_id=run_2,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_current, value_numeric, valid_to
            FROM curated_geo.observation
            WHERE feature_id = %s AND attribute_key = 'population_total'
            ORDER BY created_at
            """,
            (feature_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert rows[0][0] is False
    assert rows[0][2] is not None
    assert rows[1][0] is True
    assert rows[1][1] == 120


def test_aoi_computation_and_serving_view_cover_polygon_line_and_point(conn):
    source_id = _register_source(
        conn,
        name="aoi_source",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    block_type = feat.get_type_id(conn, "MEX", "block")
    road_type = feat.get_type_id(conn, "global", "road")
    stop_type = feat.get_type_id(conn, "global", "bus_stop")

    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="block-aoi",
        geometry_wkt="POLYGON((0 0,2 0,2 2,0 2,0 0))",
    )
    _insert_feature(
        conn,
        type_id=road_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="road-aoi",
        geometry_wkt="LINESTRING(-1 1, 3 1)",
    )
    _insert_feature(
        conn,
        type_id=stop_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="stop-aoi",
        geometry_wkt="POINT(1 1)",
    )

    project_id = _insert_project(conn)
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="study area",
        geometry_wkt="POLYGON((0 0,1.5 0,1.5 1.5,0 1.5,0 0))",
    )
    inserted = aoi.compute_aoi_features(conn, aoi_id)

    assert inserted == 3

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT overlap_basis, overlap_ratio
            FROM curated_geo.aoi_feature
            WHERE aoi_id = %s
            ORDER BY overlap_basis
            """,
            (aoi_id,),
        )
        overlaps = cur.fetchall()

    assert [row[0] for row in overlaps] == ["area", "length", "presence"]
    assert overlaps[0][1] < 1
    assert overlaps[1][1] < 1
    assert overlaps[2][1] == 1

    page = backend_for_connection(conn).serving_reader().get_features(aoi_id=aoi_id)
    features = page["features"]
    assert len(features) == 3
    assert all("overlap_ratio" in row for row in features)


# ── Row-Level Security contract tests ─────────────────────────────────────────


def test_rls_is_enabled_on_project_table(conn):
    """app_core.project must have RLS enabled (r011)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relrowsecurity
            FROM pg_class
            WHERE oid = 'app_core.project'::regclass
            """
        )
        (enabled,) = cur.fetchone()
    assert enabled is True


def test_rls_is_enabled_on_area_of_interest_table(conn):
    """curated_geo.area_of_interest must have RLS enabled (r011)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relrowsecurity
            FROM pg_class
            WHERE oid = 'curated_geo.area_of_interest'::regclass
            """
        )
        (enabled,) = cur.fetchone()
    assert enabled is True


def test_rls_tenant_isolation_policy_exists(conn):
    """tenant_isolation policy must exist on both RLS-protected tables (r011)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, tablename, policyname, permissive
            FROM pg_policies
            WHERE policyname = 'tenant_isolation'
            ORDER BY schemaname, tablename
            """
        )
        rows = {(r[0], r[1]): r[3] for r in cur.fetchall()}

    assert (
        "app_core",
        "project",
    ) in rows, "Missing tenant_isolation policy on app_core.project"
    assert (
        "curated_geo",
        "area_of_interest",
    ) in rows, "Missing tenant_isolation policy on curated_geo.area_of_interest"
    # Both policies must be PERMISSIVE — not restrictive — so that unset setting
    # allows all rows through (existing code without SET LOCAL continues to work).
    assert rows[("app_core", "project")] == "PERMISSIVE"
    assert rows[("curated_geo", "area_of_interest")] == "PERMISSIVE"


def test_rls_set_local_scopes_to_transaction(migrated_db):
    """SET LOCAL app.current_project_id must not leak outside the transaction block.

    This verifies the PgBouncer transaction-mode compatibility guarantee:
    SET LOCAL is scoped to the current transaction, so PgBouncer can safely
    return the connection to the pool after COMMIT/ROLLBACK without leaking
    the project context to a subsequent client.

    Uses a separate connection (not the `conn` fixture) so that explicit
    COMMIT calls do not break the fixture's rollback-based test isolation.
    """
    probe = psycopg2.connect(migrated_db)
    probe.autocommit = False
    try:
        project_id = str(uuid4())

        # Within a transaction: SET LOCAL should be visible.
        with probe.cursor() as cur:
            cur.execute("SET LOCAL app.current_project_id = %s", (project_id,))
            cur.execute("SELECT current_setting('app.current_project_id', true)")
            (during_tx,) = cur.fetchone()
        probe.commit()

        assert during_tx == project_id

        # After COMMIT the setting must be cleared (SET LOCAL scope ended).
        with probe.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_project_id', true)")
            (after_tx,) = cur.fetchone()
        probe.commit()

        assert after_tx is None or after_tx == ""
    finally:
        probe.rollback()
        probe.close()


def test_overlap_jobs_declare_list_and_compute_all_pairs(conn):
    """Postgres: declare_pair + compute_all_pairs end-to-end for a MEX lot→block pair."""
    source_id = _register_source(
        conn,
        name="overlap_jobs_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    block_type = feat.get_type_id(conn, "MEX", "block")
    lot_type = feat.get_type_id(conn, "MEX", "lot")

    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="blk-1",
        geometry_wkt="POLYGON((-99.140 19.430,-99.130 19.430,-99.130 19.440,-99.140 19.440,-99.140 19.430))",
    )
    _insert_feature(
        conn,
        type_id=lot_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="lot-1",
        geometry_wkt="POLYGON((-99.138 19.432,-99.134 19.432,-99.134 19.436,-99.138 19.436,-99.138 19.432))",
    )

    first = overlap_jobs.declare_pair(
        conn,
        source_context="MEX",
        source_slug="lot",
        target_context="MEX",
        target_slug="block",
    )
    second = overlap_jobs.declare_pair(
        conn,
        source_context="MEX",
        source_slug="lot",
        target_context="MEX",
        target_slug="block",
    )
    assert first == second

    pairs = overlap_jobs.list_pairs(conn)
    assert len(pairs) == 1
    assert pairs[0]["source"] == "MEX:lot"
    assert pairs[0]["target"] == "MEX:block"

    run = overlap_jobs.compute_all_pairs(conn)
    assert run["rows_total"] == 1

    # Re-run is idempotent via ON CONFLICT upsert.
    rerun = overlap_jobs.compute_all_pairs(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM curated_geo.feature_overlap")
        assert cur.fetchone()[0] == 1
    assert rerun["rows_total"] == 1

    with conn.cursor() as cur:
        cur.execute("""
            SELECT overlap_basis, overlap_ratio
            FROM curated_geo.feature_overlap
        """)
        basis, ratio = cur.fetchone()
    assert basis == "area"
    assert 0 < float(ratio) <= 1.0


def test_overlap_compute_all_pairs_scopes_by_aoi(conn):
    """aoi_id restricts computation to source features intersecting the AOI."""
    source_id = _register_source(
        conn,
        name="overlap_aoi_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    block_type = feat.get_type_id(conn, "MEX", "block")
    lot_type = feat.get_type_id(conn, "MEX", "lot")

    # Two blocks side by side.
    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="blk-L",
        geometry_wkt="POLYGON((-99.150 19.430,-99.140 19.430,-99.140 19.440,-99.150 19.440,-99.150 19.430))",
    )
    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="blk-R",
        geometry_wkt="POLYGON((-99.130 19.430,-99.120 19.430,-99.120 19.440,-99.130 19.440,-99.130 19.430))",
    )
    # A lot inside each block.
    _insert_feature(
        conn,
        type_id=lot_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="lot-L",
        geometry_wkt="POLYGON((-99.148 19.432,-99.142 19.432,-99.142 19.438,-99.148 19.438,-99.148 19.432))",
    )
    _insert_feature(
        conn,
        type_id=lot_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="lot-R",
        geometry_wkt="POLYGON((-99.128 19.432,-99.122 19.432,-99.122 19.438,-99.128 19.438,-99.128 19.432))",
    )

    overlap_jobs.declare_pair(
        conn,
        source_context="MEX",
        source_slug="lot",
        target_context="MEX",
        target_slug="block",
    )

    project_id = _insert_project(conn, name="Overlap AOI Scope Project")
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="left-aoi",
        geometry_wkt="POLYGON((-99.150 19.430,-99.140 19.430,-99.140 19.440,-99.150 19.440,-99.150 19.430))",
    )

    scoped = overlap_jobs.compute_all_pairs(conn, aoi_id=aoi_id)
    assert scoped["rows_total"] == 1

    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.source_record_ref
            FROM curated_geo.feature_overlap fo
            JOIN curated_geo.feature f ON f.id = fo.source_feature_id
        """)
        refs = [row[0] for row in cur.fetchall()]
    assert refs == ["lot-L"]

    unscoped = overlap_jobs.compute_all_pairs(conn)
    assert unscoped["rows_total"] == 2


def test_aoi_summary_upsert_list_and_serving_view(conn):
    """Postgres: AOISummaryRepository upserts via partial unique indexes; the
    serving view exposes only status='current' rows and explore.layer_catalog_v
    lists the new layers.
    """
    project_id = _insert_project(conn, name="AOI Summary Project")
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="summary-aoi",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )

    summary_repo = backend_for_connection(conn).aoi_summary_repo()
    indicator_id = str(uuid4())

    first = summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=1.0,
        aggregation_note="seed",
    )
    # Re-upsert (no scenario) → same id, updated value.
    second = summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=2.0,
        aggregation_note="reran",
    )
    assert first == second

    current = summary_repo.get_summary(aoi_id=aoi_id, indicator_id=indicator_id)
    assert current["value"] == 2.0
    assert current["aggregation_note"] == "reran"
    assert current["status"] == "current"

    scenario_id = str(uuid4())
    scenario_summary_id = summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=5.0,
        scenario_id=scenario_id,
    )
    assert scenario_summary_id != first

    base_rows = summary_repo.list_summaries(aoi_id=aoi_id)
    assert [r["value"] for r in base_rows] == [2.0]

    scenario_rows = summary_repo.list_summaries(aoi_id=aoi_id, scenario_id=scenario_id)
    assert [r["value"] for r in scenario_rows] == [5.0]

    # Marking the base-city row stale drops it from the serving view.
    summary_repo.upsert_summary(
        aoi_id=aoi_id,
        indicator_id=indicator_id,
        value=2.0,
        status="stale",
    )
    assert summary_repo.list_summaries(aoi_id=aoi_id) == []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indicator_id::text, value
            FROM serving_geo.aoi_summary_v
            WHERE aoi_id = %s
            """,
            (aoi_id,),
        )
        serving_rows = cur.fetchall()
    assert serving_rows == [(indicator_id, 5.0)]

    # Explore catalog surfaces the new layers.
    layers = backend_for_connection(conn).explore_reader().list_layers()
    names = {(row["schema_name"], row["object_name"]) for row in layers}
    assert ("curated_geo", "aoi_indicator_summary") in names
    assert ("serving_geo", "aoi_summary_v") in names


def test_summarise_aoi_end_to_end_weighted_mean_postgres(conn):
    """End-to-end Postgres coverage for the summarise_aoi worker.

    Seeds a project + AOI covering two blocks with very different areas
    and different indicator values, computes aoi_feature (so the weights
    come from real PostGIS area measurement), writes the two child
    indicator_result rows, then runs summarise_aoi and asserts:

    * the resulting summary is status='current' and the value is pulled
      toward the larger block's value — i.e. the weight is
      `aoi_feature.overlap_measure`, not `overlap_ratio`; and
    * the row surfaces through `serving_geo.aoi_summary_v` with the same
      value.
    """
    seed_catalog(conn)
    project_id = _insert_project(conn, name="AOI Summary E2E Project")

    # Small block (≈1/9 of the AOI area) with a high indicator value,
    # big block (≈8/9 of the AOI area) with a low one. An unweighted mean
    # would sit near 0.55; an overlap_ratio-weighted mean (both children
    # nested → weight ≈1.0) would give the same. A correctly area-weighted
    # mean must be close to the big block's 0.1, i.e. well below 0.3.
    source_id = _register_source(
        conn,
        name="aoi_summary_e2e_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    small_block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="small",
        geometry_wkt="POLYGON((0 0, 0.001 0, 0.001 0.001, 0 0.001, 0 0))",
    )
    big_block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="big",
        geometry_wkt="POLYGON((0.002 0, 0.005 0, 0.005 0.003, 0.002 0.003, 0.002 0))",
    )
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="summary-aoi-e2e",
        geometry_wkt="POLYGON((0 0, 0.006 0, 0.006 0.004, 0 0.004, 0 0))",
    )
    aoi.compute_aoi_features(conn, aoi_id)

    # Write indicator_result rows directly through the repo — deliberately
    # bypassing compute_block_compactness so this test is tightly scoped to
    # aoi_summary's join / weight behaviour.
    backend = backend_for_connection(conn)
    job_run_id = backend.job_run_repo().start_job(
        model_family="urban_form",
        code_version="test",
        status="running",
    )
    result_repo = backend.indicator_result_repo()
    result_repo.bulk_write_numeric_results(
        [(small_block_id, 0.9), (big_block_id, 0.1)],
        indicator_slug="urban_form_block_compactness",
        job_run_id=job_run_id,
    )
    backend.job_run_repo().complete_job(job_run_id, status="succeeded")

    result = summarise_aoi(
        conn,
        aoi_id=aoi_id,
        indicator_slug="urban_form_block_compactness",
    )
    assert result.status == "current"
    assert result.value is not None
    # Area-weighted: the big block dominates. If weighting reverted to
    # overlap_ratio, the value would sit near 0.5 and this assertion would
    # fail loudly.
    assert result.value < 0.3, (
        f"expected weighted mean near the big block's 0.1, got {result.value!r} — "
        "likely regressed to overlap_ratio weighting"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value FROM serving_geo.aoi_summary_v
            WHERE aoi_id = %s
            """,
            (aoi_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(result.value)


def test_pmtiles_export_end_to_end_against_postgres(migrated_db, tmp_path):
    """Exercise the full PMTiles export path against a real ogr2ogr subprocess.

    The export spawns a subprocess that opens its own Postgres connection,
    so it cannot see data in the usual `conn`-fixture transaction. This
    test uses an autocommit connection, seeds + exports + cleans up,
    and skips cleanly when ogr2ogr is not installed (GDAL ≥ 3.8 with the
    PMTiles driver is required).
    """
    import shutil

    if shutil.which("ogr2ogr") is None:
        pytest.skip("ogr2ogr not available; install GDAL ≥ 3.8 with the PMTiles driver")

    conn = psycopg2.connect(migrated_db)
    conn.autocommit = True
    seed_source_id: str | None = None
    output_dir = tmp_path / "pmtiles"

    try:
        with conn.cursor() as cur:
            seed_source_id = cat.register_source(
                conn,
                name="pmtiles_e2e_seed",
                provider="pytest",
                storage_type="vector",
                integration_role="canonical_entity",
                storage_uri="file:///tmp/pmtiles_e2e.geojson",
                version="v1",
            )
            run_id = cat.start_ingestion_run(
                conn,
                source_id=seed_source_id,
                orchestrator="pytest",
                orchestrator_run_id=str(uuid4()),
                transformation_step="silver_to_curated",
                code_version="test",
            )
            municipality_type = feat.get_type_id(conn, "MEX", "municipality")
            _insert_feature(
                conn,
                type_id=municipality_type,
                source_id=seed_source_id,
                ingestion_run_id=run_id,
                dataset_version="v1",
                source_record_ref="mun-e2e-1",
                geometry_wkt="POLYGON((-99.15 19.40,-99.05 19.40,-99.05 19.50,-99.15 19.50,-99.15 19.40))",
            )
            cat.complete_ingestion_run(conn, run_id, status="succeeded")

        payload = export_pmtiles_snapshot(
            dsn=migrated_db,
            layer_id="admin_reference",
            output_dir=output_dir,
            snapshot_base_url="http://localhost:3001",
        )

        artifact_path = Path(payload["artifact_path"])
        assert artifact_path.exists(), "ogr2ogr did not produce a PMTiles artifact"
        assert artifact_path.stat().st_size > 0, "PMTiles artifact is empty"
        assert payload["source_relation"] == "serving_geo.admin_v"

        latest = (
            backend_for_connection(conn)
            .tile_repo()
            .get_latest_snapshot(layer_id="admin_reference")
        )
        assert latest is not None
        assert latest["snapshot_version"] == payload["snapshot_version"]
        assert latest["status"] == "ready"

    finally:
        # Explicit cleanup: autocommit means nothing rolls back automatically.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM curated_geo.tile_snapshot_registry WHERE layer_id = 'admin_reference'"
            )
            if seed_source_id:
                cur.execute(
                    "DELETE FROM curated_geo.feature WHERE source_id = %s",
                    (seed_source_id,),
                )
                cur.execute(
                    "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
                    (seed_source_id,),
                )
                cur.execute(
                    "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
                    (seed_source_id,),
                )
        conn.close()


def test_block_compactness_end_to_end(conn):
    """Postgres: full vertical slice exercising the indicator_result trigger."""
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="compactness_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    block_type = feat.get_type_id(conn, "MEX", "block")
    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="sq",
        geometry_wkt="POLYGON((-99.140 19.430,-99.136 19.430,-99.136 19.434,-99.140 19.434,-99.140 19.430))",
    )
    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="rect",
        geometry_wkt="POLYGON((-99.130 19.430,-99.126 19.430,-99.126 19.4301,-99.130 19.4301,-99.130 19.430))",
    )

    result = compute_block_compactness(conn, code_version="test")
    assert result.rows_written == 2

    result_repo = backend_for_connection(conn).indicator_result_repo()
    current = result_repo.list_current_for_indicator(
        indicator_slug="urban_form_block_compactness"
    )
    by_ref: dict[str, float] = {}
    for row in current:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_record_ref FROM curated_geo.feature WHERE id = %s",
                (row["feature_id"],),
            )
            by_ref[cur.fetchone()[0]] = float(row["value_numeric"])
    assert 0.7 < by_ref["sq"] < 0.9, by_ref
    assert 0.0 <= by_ref["rect"] < 0.1, by_ref

    # Trigger-enforced rule: invalid value_numeric is refused server-side.
    job_repo = backend_for_connection(conn).job_run_repo()
    synthetic_job = job_repo.start_job(
        model_family="urban_form", code_version="test", status="running"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM curated_geo.feature WHERE source_record_ref = 'sq'"
        )
        sq_id = cur.fetchone()[0]
    with pytest.raises(psycopg2.Error):
        result_repo.write_result(
            feature_id=sq_id,
            indicator_slug="urban_form_block_compactness",
            job_run_id=synthetic_job,
            value_numeric=2.0,
        )
    conn.rollback()


def test_indicator_applies_to_enforced_by_trigger(conn):
    """Writing a result for a feature outside applies_to must be rejected.

    urban_form_block_compactness applies_to = ['block', 'ageb']. Writing to a
    MEX:municipality feature must be rejected by the before-write trigger.
    """
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="applies_to_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    muni_type = feat.get_type_id(conn, "MEX", "municipality")
    muni_id = _insert_feature(
        conn,
        type_id=muni_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="m-1",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )

    job_repo = backend_for_connection(conn).job_run_repo()
    job_id = job_repo.start_job(
        model_family="urban_form", code_version="test", status="running"
    )
    result_repo = backend_for_connection(conn).indicator_result_repo()
    with pytest.raises(psycopg2.Error):
        result_repo.write_result(
            feature_id=muni_id,
            indicator_slug="urban_form_block_compactness",
            job_run_id=job_id,
            value_numeric=0.5,
        )
    conn.rollback()


def test_block_building_density_end_to_end(conn):
    """Postgres: building-density vertical slice including trigger bounds."""
    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="density_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    block_type = feat.get_type_id(conn, "MEX", "block")
    building_type = feat.get_type_id(conn, "global", "building")

    dense_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="dense",
        geometry_wkt="POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,-99.140 19.433,-99.140 19.430))",
    )
    sparse_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="sparse",
        geometry_wkt="POLYGON((-99.130 19.430,-99.127 19.430,-99.127 19.433,-99.130 19.433,-99.130 19.430))",
    )
    _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="empty",
        geometry_wkt="POLYGON((-99.120 19.430,-99.117 19.430,-99.117 19.433,-99.120 19.433,-99.120 19.430))",
    )

    # Three inside 'dense', one inside 'sparse', one outside all blocks.
    for ref, lon, lat in [
        ("bd-1", -99.139, 19.431),
        ("bd-2", -99.138, 19.432),
        ("bd-3", -99.1375, 19.4315),
        ("bs-1", -99.129, 19.431),
        ("b-outside", -99.100, 19.400),
    ]:
        _insert_feature(
            conn,
            type_id=building_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=(
                f"POLYGON(({lon} {lat},{lon + 0.0001} {lat},"
                f"{lon + 0.0001} {lat + 0.0001},{lon} {lat + 0.0001},{lon} {lat}))"
            ),
        )

    result = compute_block_building_density(conn, code_version="test")
    assert result.rows_written == 3
    assert result.buildings_attributed == 4

    result_repo = backend_for_connection(conn).indicator_result_repo()
    current = {
        (row["indicator_slug"], _fetch_source_ref(conn, row["feature_id"])): float(
            row["value_numeric"]
        )
        for row in result_repo.list_current_for_indicator(
            indicator_slug="urban_form_block_building_density"
        )
    }
    assert current[("urban_form_block_building_density", "empty")] == 0.0
    assert current[("urban_form_block_building_density", "sparse")] > 0.0
    assert current[("urban_form_block_building_density", "dense")] == pytest.approx(
        3 * current[("urban_form_block_building_density", "sparse")], rel=1e-6
    )

    # Trigger-enforced valid_max: rejecting 10000/ha (catalog cap 500).
    job_repo = backend_for_connection(conn).job_run_repo()
    job_id = job_repo.start_job(
        model_family="urban_form", code_version="test", status="running"
    )
    with pytest.raises(psycopg2.Error):
        result_repo.write_result(
            feature_id=dense_id,
            indicator_slug="urban_form_block_building_density",
            job_run_id=job_id,
            value_numeric=10_000.0,
        )
    conn.rollback()

    # sanity: the never-attributed building didn't land against the empty block.
    assert current[("urban_form_block_building_density", "empty")] == 0.0
    _ = sparse_id  # keeps the variable named in case future asserts reference it


def _fetch_source_ref(conn, feature_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_record_ref FROM curated_geo.feature WHERE id = %s",
            (feature_id,),
        )
        return cur.fetchone()[0]


# ─── R1 raster-surface coverage (Postgres) ────────────────────────────────


def _r1_seed_raster_source(conn, *, name: str = "r1-test") -> tuple[str, str, str]:
    """Helper: register a raster source + ingestion_run + file_asset. Returns
    (source_id, ingestion_run_id, asset_id)."""
    src_id = cat.register_source(
        conn,
        name=name,
        provider="pytest",
        storage_type="raster",
        integration_role="enrichment",
        storage_uri="file://data/silver/x.cog.tif",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=src_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="test_seed",
        code_version="test",
    )
    asset_id = (
        backend_for_connection(conn)
        .file_asset_repo()
        .register_file_asset(
            source_id=src_id,
            ingestion_run_id=run_id,
            storage_uri="file://data/silver/x.cog.tif",
            dataset_version="v1",
            format="COG",
            crs="EPSG:4326",
            resolution_m=30.0,
        )
    )
    return src_id, run_id, asset_id


def test_r1_feature_iter_geometries_by_type_returns_wkb_per_feature(conn):
    """iter_geometries_by_type yields one (feature_id, WKB bytes) per feature;
    AOI scoping filters correctly."""
    from shapely import wkb as shp_wkb

    source_id = _register_source(
        conn,
        name="r1_iter_geom_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    # Two blocks outside + one inside the AOI.
    for i, wkt in enumerate(
        [
            "POLYGON((10 10, 11 10, 11 11, 10 11, 10 10))",  # outside AOI
            "POLYGON((20 20, 21 20, 21 21, 20 21, 20 20))",  # outside AOI
            "POLYGON((0.2 0.2, 0.4 0.2, 0.4 0.4, 0.2 0.4, 0.2 0.2))",  # inside AOI
        ]
    ):
        _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"r1-iter-{i}",
            geometry_wkt=wkt,
        )

    repo = backend_for_connection(conn).feature_repo()

    # No AOI: all three features.
    all_geoms = repo.iter_geometries_by_type(type_id=block_type)
    assert len(all_geoms) == 3
    for fid, wkb_bytes in all_geoms:
        assert isinstance(fid, str)
        assert isinstance(wkb_bytes, bytes) and len(wkb_bytes) > 0
        # WKB is parseable back to a shapely geom (i.e. valid EPSG:4326 WKB).
        assert shp_wkb.loads(wkb_bytes).is_valid

    # AOI-scoped: only the feature intersecting the AOI polygon is returned.
    project_id = _insert_project(conn, name="R1 iter_geom AOI")
    aoi_id = aoi.create_aoi(
        conn,
        project_id=project_id,
        name="r1-aoi",
        geometry_wkt="POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    )
    scoped = repo.iter_geometries_by_type(type_id=block_type, aoi_id=aoi_id)
    assert len(scoped) == 1
    assert shp_wkb.loads(scoped[0][1]).is_valid


def test_r1_file_asset_get_by_source_round_trip(conn):
    """register_file_asset then get_by_source returns the same row."""
    src_id, _run_id, asset_id = _r1_seed_raster_source(conn, name="r1_file_asset_rt")
    got = backend_for_connection(conn).file_asset_repo().get_by_source(src_id)
    assert got is not None
    assert got["id"] == asset_id
    assert got["source_id"] == src_id
    assert got["storage_uri"] == "file://data/silver/x.cog.tif"
    assert got["crs"] == "EPSG:4326"
    assert float(got["resolution_m"]) == 30.0
    assert got["format"] == "COG"

    # Unknown source → None, not an exception.
    assert (
        backend_for_connection(conn).file_asset_repo().get_by_source(str(uuid4()))
        is None
    )


def test_r1_retire_current_by_prefix_is_separator_safe(conn):
    """prefix='ndvi' retires 'ndvi' + 'ndvi_mean' but NOT 'ndvi2_mean'.

    Regression guard for the Postgres E-string escape bug the R1 smoke test
    caught: ``LIKE :prefix || E'\\_%%' ESCAPE E'\\\\'`` silently drops the
    backslash because `\\_` isn't a defined E-string escape, reverting `_` to
    a wildcard. The current implementation uses ``starts_with()`` instead.
    If a future edit reintroduces LIKE/ESCAPE, this test fires.
    """
    source_id = _register_source(
        conn,
        name="r1_retire_src",
        storage_type="tabular",
        integration_role="enrichment",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    fid = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="r1-retire-blk",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )
    obs_repo = backend_for_connection(conn).observation_repo()
    obs_repo.bulk_upsert_current(
        [
            {
                "feature_id": fid,
                "attribute_key": "ndvi",
                "value": 0.7,
                "value_type": "numeric",
            },
            {
                "feature_id": fid,
                "attribute_key": "ndvi_mean",
                "value": 0.5,
                "value_type": "numeric",
            },
            {
                "feature_id": fid,
                "attribute_key": "ndvi2_mean",
                "value": 0.9,
                "value_type": "numeric",
            },
        ],
        source_id=source_id,
        ingestion_run_id=run_id,
    )

    retired = obs_repo.retire_current_by_prefix(prefix="ndvi")
    assert retired == 2  # 'ndvi' + 'ndvi_mean', NOT 'ndvi2_mean'

    with conn.cursor() as cur:
        cur.execute(
            "SELECT attribute_key, is_current FROM curated_geo.observation "
            "WHERE feature_id = %s::uuid ORDER BY attribute_key",
            (fid,),
        )
        rows = {k: v for k, v in cur.fetchall()}
    assert rows["ndvi"] is False
    assert rows["ndvi_mean"] is False
    assert rows["ndvi2_mean"] is True, "ndvi2_mean must NOT have been retired"


def test_r1_retire_current_by_prefix_rejects_empty(conn):
    obs_repo = backend_for_connection(conn).observation_repo()
    with pytest.raises(ValueError, match="non-empty"):
        obs_repo.retire_current_by_prefix(prefix="")


def test_r1_has_active_prefix_excludes_own_source(conn):
    """Two sources writing under the same prefix: has_active_prefix(..,
    excluding_source_id=A) sees B's current row; excluding=B sees A's;
    excluding both (via a third UUID) sees whichever remains."""
    src_a = _register_source(
        conn,
        name="r1_active_a",
        storage_type="tabular",
        integration_role="enrichment",
    )
    run_a = _start_run(conn, src_a)
    block_type = feat.get_type_id(conn, "MEX", "block")
    fid = _insert_feature(
        conn,
        type_id=block_type,
        source_id=src_a,
        ingestion_run_id=run_a,
        dataset_version="v1",
        source_record_ref="r1-active-blk",
        geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
    )
    obs_repo = backend_for_connection(conn).observation_repo()
    obs_repo.bulk_upsert_current(
        [
            {
                "feature_id": fid,
                "attribute_key": "lst_mean",
                "value": 300.0,
                "value_type": "numeric",
            }
        ],
        source_id=src_a,
        ingestion_run_id=run_a,
    )

    # Excluding A → no other source has it → False.
    assert obs_repo.has_active_prefix(prefix="lst", excluding_source_id=src_a) is False
    # Excluding a random UUID → A still owns it → True.
    assert (
        obs_repo.has_active_prefix(
            prefix="lst",
            excluding_source_id=str(uuid4()),
        )
        is True
    )
    # Exact prefix match also counts (attribute_key = 'lst_mean' starts with 'lst_').
    assert (
        obs_repo.has_active_prefix(
            prefix="lst_mean",
            excluding_source_id=str(uuid4()),
        )
        is True
    )
    # Separator-safe: prefix='ls' must NOT match 'lst_mean'.
    assert (
        obs_repo.has_active_prefix(
            prefix="ls",
            excluding_source_id=str(uuid4()),
        )
        is False
    )


def test_r1_raster_layer_repo_lifecycle(conn):
    """register → get_by_source → list_published_layers → set_published(False)
    → list no longer includes the layer."""
    src_id, _run_id, asset_id = _r1_seed_raster_source(conn, name="r1_layer_lc")
    repo = backend_for_connection(conn).raster_layer_repo()

    layer_id = repo.register_layer(
        source_id=src_id,
        asset_id=asset_id,
        layer_slug="r1_lifecycle",
        logical_name="R1 lifecycle test",
        aggregation_kind="intensive",
        colormap_name="viridis",
        rescale="0,1",
    )
    assert layer_id

    fetched = repo.get_by_source(src_id)
    assert fetched is not None
    assert fetched["layer_slug"] == "r1_lifecycle"
    assert fetched["aggregation_kind"] == "intensive"
    assert fetched["colormap_name"] == "viridis"
    assert fetched["rescale"] == "0,1"
    assert fetched["published"] is True
    # Joined columns from serving_geo.raster_layer_v must be populated.
    assert fetched["storage_uri"] == "file://data/silver/x.cog.tif"
    assert fetched["crs"] == "EPSG:4326"

    published = repo.list_published_layers()
    assert any(r["layer_slug"] == "r1_lifecycle" for r in published)

    repo.set_published(layer_slug="r1_lifecycle", published=False)
    published_after = repo.list_published_layers()
    assert not any(r["layer_slug"] == "r1_lifecycle" for r in published_after)
    # But get_by_source still returns it (published=false; reads aren't gated).
    still_there = repo.get_by_source(src_id)
    assert still_there is not None and still_there["published"] is False


def test_r1_raster_layer_aggregation_kind_check_constraint(conn):
    """The CHECK on aggregation_kind enforces {'intensive','extensive'}."""
    src_id, _run_id, asset_id = _r1_seed_raster_source(conn, name="r1_agg_check")
    repo = backend_for_connection(conn).raster_layer_repo()
    with pytest.raises(psycopg2.errors.CheckViolation):
        repo.register_layer(
            source_id=src_id,
            asset_id=asset_id,
            layer_slug="r1_bad_kind",
            aggregation_kind="categorical",
        )
    conn.rollback()
