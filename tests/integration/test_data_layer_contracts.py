from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from reimagina.data import aoi
from reimagina.data import catalog as cat
from reimagina.data import explore
from reimagina.data import feature as feat
from reimagina.data import file_asset
from reimagina.data import observation as obs
from reimagina.data import serving
from reimagina.data import tiles as tile_data
from reimagina.jobs import overlap as overlap_jobs
from reimagina.pipeline import loader
from reimagina.storage import backend_for_connection
from reimagina.tiles.service import export_pmtiles_snapshot


def _register_source(
    conn,
    *,
    name: str,
    storage_type: str,
    integration_role: str,
) -> str:
    return cat.register_source(
        conn,
        name=name,
        provider="test",
        storage_type=storage_type,
        integration_role=integration_role,
        storage_uri=f"file:///tmp/{name}.dat",
        version="v1",
    )


def _start_run(conn, source_id: str, step: str = "test_load") -> str:
    return cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step=step,
        code_version="test",
    )


def _insert_feature(
    conn,
    *,
    type_id: str,
    source_id: str,
    ingestion_run_id: str,
    dataset_version: str,
    source_record_ref: str,
    geometry_wkt: str,
    parent_id: str | None = None,
    feature_id: str | None = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (id, type_id, parent_id, name, geometry, attributes,
               source_id, source_record_ref, ingestion_run_id, dataset_version)
            VALUES (
              COALESCE(%s::uuid, gen_random_uuid()),
              %s, %s, %s,
              ST_SetSRID(ST_GeomFromText(%s), 4326),
              '{}'::jsonb,
              %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                feature_id,
                type_id,
                parent_id,
                source_record_ref,
                geometry_wkt,
                source_id,
                source_record_ref,
                ingestion_run_id,
                dataset_version,
            ),
        )
        return str(cur.fetchone()[0])


def _insert_project(conn, name: str = "Test Project") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_core.project (name, description)
            VALUES (%s, %s)
            RETURNING id
            """,
            (name, "integration test project"),
        )
        return str(cur.fetchone()[0])


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

    layers = explore.get_layers(conn)
    assert any(row["logical_name"] == "feature_type_stats" for row in layers)

    sources = explore.get_sources(conn)
    assert any(row["source_id"] == source_id and row["ingestion_run_id"] == run_id for row in sources)

    feature_stats = explore.get_feature_stats(conn)
    assert any(
        row["source_id"] == source_id
        and row["type_slug"] == "building"
        and row["row_count"] == 1
        for row in feature_stats
    )

    serving_stats = explore.get_serving_stats(conn)
    assert any(row["stat_scope"] == "all" and row["row_count"] >= 1 for row in serving_stats)


def test_tile_snapshot_registry_and_source_resolution(conn):
    latest = tile_data.get_latest_tile_snapshot(conn, layer_id="admin_reference")
    assert latest is None

    snapshot_id = backend_for_connection(conn).tile_repo().register_snapshot(
        layer_id="admin_reference",
        artifact_path="/tmp/admin_reference-20260415T120000Z.pmtiles",
        artifact_uri="http://localhost:3001/admin_reference-20260415T120000Z.pmtiles",
        snapshot_version="20260415T120000Z",
        source_relation="serving_geo.admin_v",
        source_lineage_ref={"latest_successful_ingestion_run_id": None},
    )
    assert snapshot_id

    latest = tile_data.get_latest_tile_snapshot(conn, layer_id="admin_reference")
    assert latest is not None
    assert latest["layer_id"] == "admin_reference"

    sources = tile_data.get_tile_sources(
        conn,
        live_tilejson_base_url="http://localhost:3000/get_tile",
        snapshot_base_url="http://localhost:3001",
    )
    by_id = {row["layer_id"]: row for row in sources}
    assert by_id["admin_reference"]["source_mode"] == "pmtiles_snapshot"
    assert by_id["admin_reference"]["available"] is True
    assert by_id["admin_reference"]["source_config"]["url"].endswith(".pmtiles")
    assert by_id["baseline_live"]["source_mode"] == "live_mvt"
    assert by_id["baseline_live"]["source_config"]["tilejson_url"] == "http://localhost:3000/get_tile"


def test_pmtiles_export_registers_snapshot_metadata(conn, monkeypatch, tmp_path):
    output_dir = tmp_path / "pmtiles"

    def fake_run_export(command: list[str]) -> None:
        Path(command[3]).write_text("pmtiles-placeholder")

    @contextmanager
    def fake_connect(_dsn: str):
        yield conn

    monkeypatch.setattr("reimagina.tiles.service._run_export_command", fake_run_export)
    monkeypatch.setattr("reimagina.tiles.service.connect", fake_connect)

    payload = export_pmtiles_snapshot(
        dsn="postgresql://localhost/reimagina_test",
        layer_id="admin_reference",
        output_dir=output_dir,
        snapshot_base_url="http://localhost:3001",
        snapshot_version="20260415T130000Z",
    )

    assert payload["layer_id"] == "admin_reference"
    assert Path(payload["artifact_path"]).exists()

    latest = tile_data.get_latest_tile_snapshot(conn, layer_id="admin_reference")
    assert latest is not None
    assert latest["snapshot_version"] == "20260415T130000Z"
    assert latest["artifact_uri"] == "http://localhost:3001/admin_reference-20260415T130000Z.pmtiles"


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

    page = serving.get_features(conn, aoi_id=aoi_id)
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

    assert ("app_core", "project") in rows, "Missing tenant_isolation policy on app_core.project"
    assert ("curated_geo", "area_of_interest") in rows, (
        "Missing tenant_isolation policy on curated_geo.area_of_interest"
    )
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
        source_context="MEX", source_slug="lot",
        target_context="MEX", target_slug="block",
    )
    second = overlap_jobs.declare_pair(
        conn,
        source_context="MEX", source_slug="lot",
        target_context="MEX", target_slug="block",
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
    _insert_feature(conn, type_id=block_type, source_id=source_id, ingestion_run_id=run_id,
        dataset_version="v1", source_record_ref="blk-L",
        geometry_wkt="POLYGON((-99.150 19.430,-99.140 19.430,-99.140 19.440,-99.150 19.440,-99.150 19.430))")
    _insert_feature(conn, type_id=block_type, source_id=source_id, ingestion_run_id=run_id,
        dataset_version="v1", source_record_ref="blk-R",
        geometry_wkt="POLYGON((-99.130 19.430,-99.120 19.430,-99.120 19.440,-99.130 19.440,-99.130 19.430))")
    # A lot inside each block.
    _insert_feature(conn, type_id=lot_type, source_id=source_id, ingestion_run_id=run_id,
        dataset_version="v1", source_record_ref="lot-L",
        geometry_wkt="POLYGON((-99.148 19.432,-99.142 19.432,-99.142 19.438,-99.148 19.438,-99.148 19.432))")
    _insert_feature(conn, type_id=lot_type, source_id=source_id, ingestion_run_id=run_id,
        dataset_version="v1", source_record_ref="lot-R",
        geometry_wkt="POLYGON((-99.128 19.432,-99.122 19.432,-99.122 19.438,-99.128 19.438,-99.128 19.432))")

    overlap_jobs.declare_pair(
        conn,
        source_context="MEX", source_slug="lot",
        target_context="MEX", target_slug="block",
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
        aoi_id=aoi_id, indicator_id=indicator_id, value=1.0,
        aggregation_note="seed",
    )
    # Re-upsert (no scenario) → same id, updated value.
    second = summary_repo.upsert_summary(
        aoi_id=aoi_id, indicator_id=indicator_id, value=2.0,
        aggregation_note="reran",
    )
    assert first == second

    current = summary_repo.get_summary(aoi_id=aoi_id, indicator_id=indicator_id)
    assert current["value"] == 2.0
    assert current["aggregation_note"] == "reran"
    assert current["status"] == "current"

    scenario_id = str(uuid4())
    scenario_summary_id = summary_repo.upsert_summary(
        aoi_id=aoi_id, indicator_id=indicator_id, value=5.0,
        scenario_id=scenario_id,
    )
    assert scenario_summary_id != first

    base_rows = summary_repo.list_summaries(aoi_id=aoi_id)
    assert [r["value"] for r in base_rows] == [2.0]

    scenario_rows = summary_repo.list_summaries(aoi_id=aoi_id, scenario_id=scenario_id)
    assert [r["value"] for r in scenario_rows] == [5.0]

    # Marking the base-city row stale drops it from the serving view.
    summary_repo.upsert_summary(
        aoi_id=aoi_id, indicator_id=indicator_id, value=2.0, status="stale",
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
    layers = explore.get_layers(conn)
    names = {(row["schema_name"], row["object_name"]) for row in layers}
    assert ("curated_geo", "aoi_indicator_summary") in names
    assert ("serving_geo", "aoi_summary_v") in names
