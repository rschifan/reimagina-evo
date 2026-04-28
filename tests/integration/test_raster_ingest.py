"""Integration tests for reimagina.pipeline.raster.ingest_raster_source.

Exercises the full pipeline against the migrated Postgres test DB:
- source registered with storage_type='raster'
- ingestion_run completes 'succeeded'
- file_asset row written with CRS + resolution_m + format='COG'
- raster_layer row written with the right aggregation_kind + layer_slug
- silver COG on disk, valid via rasterio, correct pixel dims
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from reimagina.pipeline.raster import (
    IngestResult,
    UnsupportedRasterError,
    ingest_raster_source,
)
from reimagina.storage import backend_for_connection


def _write_tiny_utm_raster(path: Path) -> Path:
    """3×3 single-band GeoTIFF in EPSG:32614, 30m pixels, metric."""
    data = np.arange(9, dtype="float32").reshape(3, 3)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=3,
        width=3,
        count=1,
        dtype="float32",
        crs="EPSG:32614",
        transform=from_origin(500_000, 2_151_000, 30.0, 30.0),
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


def _write_tiny_4326_raster(path: Path) -> Path:
    """3×3 single-band GeoTIFF in EPSG:4326 (angular)."""
    data = np.arange(9, dtype="float32").reshape(3, 3)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=3,
        width=3,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-99.15, 19.45, 0.01, 0.01),
        nodata=-999.0,
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect bronze + silver writes into ``tmp_path`` so test runs
    don't touch the real repo's ``data/`` tree.

    Existing pipeline tests don't bother with this because they rely
    on DB rollback to clean state — but bronze/silver write to the
    actual filesystem, and those writes don't get rolled back.

    Implementation: patch the module-level ``BRONZE_DIR`` / ``SILVER_DIR``
    constants directly (via monkeypatch.setattr) instead of reloading
    the modules. Module reload creates fresh class objects, which
    breaks ``isinstance`` checks and ``pytest.raises`` against classes
    imported at test-file level (the test holds a reference to the
    pre-reload class; the module returns a post-reload instance).
    """
    import reimagina.pipeline.bronze as bronze_mod
    import reimagina.pipeline.silver as silver_mod
    import reimagina.pipeline.raster as raster_mod

    bronze_dir = tmp_path / "bronze"
    silver_dir = tmp_path / "silver"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    silver_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(bronze_mod, "BRONZE_DIR", bronze_dir)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    # pipeline.raster imports SILVER_DIR at module load with
    # ``from .silver import SILVER_DIR``; the import binds at that
    # moment, so patching silver_mod.SILVER_DIR alone doesn't reach
    # raster_mod.SILVER_DIR. Patch both.
    monkeypatch.setattr(raster_mod, "SILVER_DIR", silver_dir)
    # tiles.defaults.host_data_root reads REIMAGINA_DATA_DIR on every
    # call — setting the env var covers any raster-URI path check.
    monkeypatch.setenv("REIMAGINA_DATA_DIR", str(tmp_path))

    yield tmp_path


def test_ingest_utm_raster_end_to_end(conn, isolated_data_dir, tmp_path):
    """Happy path on a metric (EPSG:32614) input.

    Covers the most common LST/NDVI case: metric CRS, north-up, square
    pixels → resolution_m populated.
    """
    raw = _write_tiny_utm_raster(tmp_path / "raw_utm.tif")

    result = ingest_raster_source(
        conn,
        raw_path=raw,
        source_name="integration-test-utm",
        provider="pytest",
        version="v1",
        layer_slug="integ_utm",
        aggregation_kind="intensive",
        colormap_name="viridis",
        rescale="0,10",
    )
    assert isinstance(result, IngestResult)
    assert result.source_id
    assert result.asset_id
    assert result.layer_id
    assert result.crs == "EPSG:32614"
    assert result.resolution_m == pytest.approx(30.0)
    assert result.storage_uri.startswith("file://data/silver/")
    assert result.storage_uri.endswith(".cog.tif")
    assert result.silver_path.exists()
    # The silver file sits under the isolated data root.
    assert isolated_data_dir in result.silver_path.parents

    # Silver artifact is a valid COG.
    with rasterio.open(result.silver_path) as ds:
        assert ds.count == 1
        assert ds.crs.to_string() == "EPSG:32614"
        assert ds.read(1).shape == (3, 3)
        assert ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"

    # DB state: source + file_asset + raster_layer all consistent.
    backend = backend_for_connection(conn)
    asset = backend.file_asset_repo().get_by_source(result.source_id)
    assert asset is not None
    assert asset["id"] == result.asset_id
    assert asset["storage_uri"] == result.storage_uri
    assert asset["format"] == "COG"
    assert asset["crs"] == "EPSG:32614"
    assert float(asset["resolution_m"]) == pytest.approx(30.0)

    # data_source_catalog.storage_uri must be rewritten from the '<pending>'
    # placeholder in Step 1 to the real silver URI — matches the convention
    # every other pipeline follows so explore.source_runs_v shows useful
    # lineage for rasters too.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_uri FROM curated_geo.data_source_catalog WHERE id = %s",
            (result.source_id,),
        )
        (catalog_uri,) = cur.fetchone()
    assert catalog_uri == result.storage_uri
    assert "<pending>" not in catalog_uri

    layer = backend.raster_layer_repo().get_by_source(result.source_id)
    assert layer is not None
    assert layer["layer_slug"] == "integ_utm"
    assert layer["aggregation_kind"] == "intensive"
    assert layer["colormap_name"] == "viridis"
    assert layer["rescale"] == "0,10"
    assert layer["published"] is True

    # Ingestion run completed 'succeeded' with useful details.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, details FROM curated_geo.ingestion_run WHERE id = %s",
            (result.ingestion_run_id,),
        )
        status, details = cur.fetchone()
    assert status == "succeeded"
    assert details["layer_slug"] == "integ_utm"
    assert details["crs"] == "EPSG:32614"


def test_ingest_4326_raster_leaves_resolution_m_null(conn, isolated_data_dir, tmp_path):
    """Angular CRS → resolution_m NULL (design-doc rule: don't invent
    a misleading metre-equivalent for degree units)."""
    raw = _write_tiny_4326_raster(tmp_path / "raw_4326.tif")

    result = ingest_raster_source(
        conn,
        raw_path=raw,
        source_name="integration-test-4326",
        provider="pytest",
        version="v1",
        layer_slug="integ_4326",
    )
    assert result.crs == "EPSG:4326"
    assert result.resolution_m is None

    backend = backend_for_connection(conn)
    asset = backend.file_asset_repo().get_by_source(result.source_id)
    assert asset["resolution_m"] is None
    assert asset["crs"] == "EPSG:4326"


def test_ingest_failure_after_silver_still_records_failed_run(
    conn,
    isolated_data_dir,
    tmp_path,
):
    """If a SQL error fires AFTER silver creation (simulated here via
    a duplicate layer_slug collision), the ingestion_run row must still
    be marked ``failed`` — not rolled back alongside the aborted
    transaction and not left in ``running``. This catches the original
    bug where the run row shared a transaction with downstream writes.
    """
    raw_a = _write_tiny_utm_raster(tmp_path / "collide_a.tif")
    raw_b = _write_tiny_utm_raster(tmp_path / "collide_b.tif")

    # Seed: successful ingest takes the slug.
    ingest_raster_source(
        conn,
        raw_path=raw_a,
        source_name="collide-a",
        provider="pytest",
        version="v1",
        layer_slug="slug_collide",
    )

    # Second ingest with the same slug must fail at the raster_layer
    # INSERT, which is Step 7 — well after the bronze + silver files are
    # on disk. The pipeline's exception handler must still write the
    # failed run row.
    with pytest.raises(Exception):
        ingest_raster_source(
            conn,
            raw_path=raw_b,
            source_name="collide-b",
            provider="pytest",
            version="v1",
            layer_slug="slug_collide",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.status
            FROM curated_geo.ingestion_run r
            JOIN curated_geo.data_source_catalog s ON s.id = r.source_id
            WHERE s.name = %s
            """,
            ("collide-b",),
        )
        rows = cur.fetchall()
    assert rows, "run row for the failing ingest was rolled back with the transaction"
    assert all(r[0] == "failed" for r in rows), f"expected 'failed', got {rows!r}"

    # Clean up to keep the shared test DB tidy.
    with conn.cursor() as cur:
        for name in ("collide-a", "collide-b"):
            cur.execute(
                "DELETE FROM curated_geo.raster_layer WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                (name,),
            )
            cur.execute(
                "DELETE FROM curated_geo.file_asset WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                (name,),
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                (name,),
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                (name,),
            )
    conn.commit()


def test_ingest_failure_marks_run_failed(conn, isolated_data_dir, tmp_path):
    """A multi-band input must leave no half-committed state: no source
    row persisted if the validation fires before any DB work."""
    raw = tmp_path / "two_band.tif"
    data = np.arange(9, dtype="float32").reshape(3, 3)
    with rasterio.open(
        raw,
        "w",
        driver="GTiff",
        height=3,
        width=3,
        count=2,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-99.15, 19.45, 0.01, 0.01),
    ) as dst:
        dst.write(data, 1)
        dst.write(data, 2)

    with pytest.raises(UnsupportedRasterError):
        ingest_raster_source(
            conn,
            raw_path=raw,
            source_name="should-not-land",
            provider="pytest",
            version="v1",
            layer_slug="multiband_fail",
        )

    # The up-front band check fires BEFORE cat.register_source, so no
    # partial state exists in the DB.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM curated_geo.data_source_catalog WHERE name = %s",
            ("should-not-land",),
        )
        assert cur.fetchone()[0] == 0


def test_cli_raster_list_against_real_db(
    conn, isolated_data_dir, tmp_path, monkeypatch
):
    """Exercises the `raster list` CLI path against a migrated DB.

    Covers the explore.raster_layer_v read + the _cursor_compat shim
    in apps/cli/raster.py (which unit tests stub out via fakes).
    """
    from apps.cli.raster import list_raster_layers

    # Seed: one ingested raster.
    raw = _write_tiny_utm_raster(tmp_path / "cli_list.tif")
    ingest_raster_source(
        conn,
        raw_path=raw,
        source_name="cli-list-test",
        provider="pytest",
        version="v1",
        layer_slug="cli_list_layer",
        aggregation_kind="extensive",
    )
    conn.commit()  # list_raster_layers opens its own connection via connect()

    # Point list_raster_layers at the same DSN the test fixture uses.
    dsn = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/reimagina_test")

    try:
        result = list_raster_layers(dsn=dsn)
        assert result["dsn"] == dsn
        slugs = [row["layer_slug"] for row in result["layers"]]
        assert "cli_list_layer" in slugs

        row = next(r for r in result["layers"] if r["layer_slug"] == "cli_list_layer")
        assert row["aggregation_kind"] == "extensive"
        assert row["published"] is True
        assert row["format"] == "COG"
    finally:
        # Roll the ingested state back so this test doesn't pollute
        # other tests using the shared DB.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM curated_geo.raster_layer WHERE layer_slug = %s",
                ("cli_list_layer",),
            )
            cur.execute(
                "DELETE FROM curated_geo.file_asset WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                ("cli-list-test",),
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                ("cli-list-test",),
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                ("cli-list-test",),
            )
        conn.commit()
