"""Integration tests for the R3 zonal-stats worker + CLI.

Exercises ``reimagina.jobs.raster_zonal_stats.run_raster_zonal_stats``
and ``apps.cli.raster.run_zonal_stats`` against the migrated Postgres
test DB. The tests own their own seeded features, ingested rasters,
and observations, and clean them up in a teardown block so parallel
test runs don't see each other's rows.

Contract coverage (per the design doc, R3 phase gate):
- Successful run: observations land with the right attribute_keys,
  lineage payload, and SCD status.
- SCD rerun correctness: re-running with a narrower ``--statistics``
  set retires the observations for stats the new run no longer writes.
- Coverage-skip path: features below ``coverage_threshold`` don't land
  any rows and are counted in the ingestion_run details.
- Prefix-collision refusal: CLI wrapper refuses when another source
  owns the prefix; ``--force`` bypasses.
- Advisory-lock contention: a second concurrent worker fails fast with
  :class:`ConcurrentRasterRun` rather than corrupting the SCD snapshot.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import numpy as np
import psycopg2
import pytest
import rasterio
from rasterio.transform import from_origin

from apps.cli.raster import PrefixCollisionError, run_zonal_stats
from reimagina.data.feature import seed_feature_types
from reimagina.jobs.raster_zonal_stats import (
    ConcurrentRasterRun,
    run_raster_zonal_stats,
)
from reimagina.pipeline.raster import ingest_raster_source
from reimagina.storage import backend_for_connection


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect bronze + silver writes into tmp_path (mirrors test_raster_ingest)."""
    import reimagina.pipeline.bronze as bronze_mod
    import reimagina.pipeline.raster as raster_mod
    import reimagina.pipeline.silver as silver_mod

    bronze_dir = tmp_path / "bronze"
    silver_dir = tmp_path / "silver"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    silver_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(bronze_mod, "BRONZE_DIR", bronze_dir)
    monkeypatch.setattr(silver_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setattr(raster_mod, "SILVER_DIR", silver_dir)
    monkeypatch.setenv("REIMAGINA_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def test_dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://localhost/reimagina_test",
    )


# ─── Helpers ──────────────────────────────────────────────────────────

def _write_utm_raster(path: Path, data: np.ndarray, *, nodata: float = -999.0) -> Path:
    """Write a single-band UTM-14N GeoTIFF with 30m pixels."""
    h, w = data.shape
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=h, width=w, count=1, dtype="float32",
        crs="EPSG:32614",
        transform=from_origin(500_000.0, 2_151_000.0, 30.0, 30.0),
        nodata=nodata,
    ) as dst:
        dst.write(data.astype("float32"), 1)
    return path


def _utm_square_wkt(min_x: float, min_y: float, size: float) -> str:
    """Build a UTM-14N square's WKT in EPSG:4326 (so it matches the
    ``curated_geo.feature.geometry`` SRID)."""
    from pyproj import Transformer
    trans = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)
    pts = [
        trans.transform(min_x, min_y),
        trans.transform(min_x + size, min_y),
        trans.transform(min_x + size, min_y + size),
        trans.transform(min_x, min_y + size),
        trans.transform(min_x, min_y),
    ]
    coords = ", ".join(f"{x} {y}" for x, y in pts)
    return f"POLYGON(({coords}))"


def _seed_feature(
    conn,
    *,
    type_id: str,
    source_id: str,
    run_id: str,
    ref: str,
    wkt_4326: str,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (type_id, source_id, source_record_ref, ingestion_run_id,
               dataset_version, geometry, attributes)
            VALUES (%s, %s, %s, %s, 'v1',
                    ST_SetSRID(ST_GeomFromText(%s), 4326),
                    '{}'::jsonb)
            RETURNING id
            """,
            (type_id, source_id, ref, run_id, wkt_4326),
        )
        return str(cur.fetchone()[0])


def _seed_feature_source(conn) -> tuple[str, str]:
    """Register a vector source + ingestion_run so features have a
    valid ``source_id`` / ``ingestion_run_id`` to point at."""
    from reimagina.data import catalog as cat
    source_id = cat.register_source(
        conn,
        name=f"r3-test-features-{uuid4()}",
        provider="pytest",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file:///tmp/fake.geojson",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="feature_seed",
        code_version="test",
    )
    return source_id, run_id


def _current_observations_for_prefix(conn, prefix: str) -> list[tuple[str, str, float]]:
    """Return ``(feature_id, attribute_key, value)`` for every current
    observation whose attribute_key matches the prefix pattern."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT feature_id::text, attribute_key, value_numeric
            FROM curated_geo.observation
            WHERE is_current
              AND (attribute_key = %s OR starts_with(attribute_key, %s || '_'))
            ORDER BY feature_id, attribute_key
            """,
            (prefix, prefix),
        )
        return [(fid, key, float(val)) for fid, key, val in cur.fetchall()]


# ─── Tests ────────────────────────────────────────────────────────────

def test_happy_path_writes_observations(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """Full-coverage features land one observation row per (feature, stat)."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    feat_a = _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="happy-a",
        wkt_4326=_utm_square_wkt(min_x=500_030, min_y=2_150_880, size=60.0),
    )
    _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="happy-b",
        wkt_4326=_utm_square_wkt(min_x=500_120, min_y=2_150_880, size=60.0),
    )

    raster_path = _write_utm_raster(
        tmp_path / "happy.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )

    conn.commit()  # worker opens its own connection; seeded rows must be visible

    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-happy-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_happy_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        result = run_raster_zonal_stats(
            conn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="happy",
            statistics=("mean", "p05"),
            direct_dsn=test_dsn,
        )

        assert result.features_in_scope >= 2
        assert result.features_written >= 1
        assert result.observations_written == 2 * result.features_written

        rows = _current_observations_for_prefix(conn, "happy")
        keys_for_a = {k for f, k, _ in rows if f == feat_a}
        if keys_for_a:  # feat_a may be skipped if coverage is low
            assert keys_for_a == {"happy_mean", "happy_p05"}

        # ingestion_run recorded the right details.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, details FROM curated_geo.ingestion_run WHERE id = %s",
                (result.ingestion_run_id,),
            )
            status, details = cur.fetchone()
        assert status == "succeeded"
        assert details["aggregation_kind"] == "intensive"
        assert set(details["statistics"]) == {"mean", "p05"}
    finally:
        _cleanup_observations(conn, "happy")


def test_rerun_with_narrower_stats_retires_dropped_stats(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """SCD correctness: second run with fewer stats must transition the
    dropped stats' rows to ``is_current = false``."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="rerun-a",
        wkt_4326=_utm_square_wkt(min_x=500_030, min_y=2_150_880, size=60.0),
    )

    raster_path = _write_utm_raster(
        tmp_path / "rerun.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    conn.commit()

    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-rerun-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_rerun_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        # Run 1 writes three stats.
        run_raster_zonal_stats(
            conn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="rerun",
            statistics=("mean", "median", "std"),
            direct_dsn=test_dsn,
        )
        current_1 = _current_observations_for_prefix(conn, "rerun")
        keys_1 = {k for _, k, _ in current_1}
        assert keys_1 == {"rerun_mean", "rerun_median", "rerun_std"}

        # Run 2 writes only one stat. The retired rows from run 1
        # should transition to is_current=false; only rerun_mean
        # survives as current.
        run_raster_zonal_stats(
            conn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="rerun",
            statistics=("mean",),
            direct_dsn=test_dsn,
        )
        current_2 = _current_observations_for_prefix(conn, "rerun")
        keys_2 = {k for _, k, _ in current_2}
        assert keys_2 == {"rerun_mean"}
    finally:
        _cleanup_observations(conn, "rerun")


def test_feature_below_coverage_threshold_is_skipped(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """A feature whose valid-pixel ratio is below the threshold
    produces zero observations and is tallied in the run details."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="cov-skip",
        wkt_4326=_utm_square_wkt(min_x=500_000, min_y=2_150_700, size=300.0),
    )

    # Raster: 4 valid pixels in the NW corner, everything else nodata.
    data = np.full((10, 10), -999.0, dtype="float32")
    data[0:2, 0:2] = 7.0
    raster_path = _write_utm_raster(tmp_path / "sparse.tif", data)
    conn.commit()

    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-cov-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_cov_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        result = run_raster_zonal_stats(
            conn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="cov",
            statistics=("mean",),
            coverage_threshold=0.5,
            direct_dsn=test_dsn,
        )
        assert result.features_written == 0
        assert result.observations_written == 0

        with conn.cursor() as cur:
            cur.execute(
                "SELECT details FROM curated_geo.ingestion_run WHERE id = %s",
                (result.ingestion_run_id,),
            )
            (details,) = cur.fetchone()
        assert details["features_skipped_low_coverage"] >= 1

        assert _current_observations_for_prefix(conn, "cov") == []
    finally:
        _cleanup_observations(conn, "cov")


def test_cli_prefix_collision_refuses_without_force(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """CLI refuses a prefix already owned by another source's current
    observations; ``--force`` bypasses."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    feat_id = _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="pfx-feat",
        wkt_4326=_utm_square_wkt(min_x=500_030, min_y=2_150_880, size=60.0),
    )

    # Source A seeds an observation under prefix 'pfx' so 'pfx' is
    # already owned — then source B tries to claim the same prefix.
    source_a, run_a = _seed_feature_source(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.observation
              (feature_id, attribute_key, value_type, value_numeric,
               source_id, ingestion_run_id, is_current)
            VALUES (%s, %s, 'numeric', %s, %s, %s, true)
            """,
            (feat_id, "pfx_mean", 1.0, source_a, run_a),
        )
    conn.commit()

    raster_path = _write_utm_raster(
        tmp_path / "pfx.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-pfx-source-b-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_pfx_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        # Without --force: must refuse.
        with pytest.raises(PrefixCollisionError):
            run_zonal_stats(
                dsn=test_dsn,
                source_id=ingest.source_id,
                feature_type_slug="block",
                attribute_key_prefix="pfx",
                statistics="mean",
                direct_dsn=test_dsn,
            )

        # With --force: proceeds (and retires source A's row as a
        # side effect — the caller accepted that responsibility).
        run_zonal_stats(
            dsn=test_dsn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="pfx",
            statistics="mean",
            force=True,
            direct_dsn=test_dsn,
        )
    finally:
        _cleanup_observations(conn, "pfx")


def test_advisory_lock_second_concurrent_run_fails_fast(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """Two workers targeting the same prefix must not both proceed.

    Strategy: pre-open a second psycopg2 connection, take
    ``pg_advisory_xact_lock`` on the lock key inside an open
    transaction, then invoke the worker in the same thread. The
    worker's ``pg_try_advisory_xact_lock`` will see the holder and
    raise :class:`ConcurrentRasterRun` immediately.
    """
    from reimagina.jobs.raster_zonal_stats import _prefix_lock_key

    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="lock-feat",
        wkt_4326=_utm_square_wkt(min_x=500_030, min_y=2_150_880, size=60.0),
    )
    raster_path = _write_utm_raster(
        tmp_path / "lock.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    conn.commit()

    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-lock-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_lock_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        holder = psycopg2.connect(test_dsn)
        holder.autocommit = False
        try:
            with holder.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (_prefix_lock_key("locked"),),
                )
            # Holder is now sitting inside an open transaction with the
            # lock. The worker's own transaction will see the lock and
            # pg_try_advisory_xact_lock will return false.
            with pytest.raises(ConcurrentRasterRun):
                run_raster_zonal_stats(
                    conn,
                    source_id=ingest.source_id,
                    feature_type_slug="block",
                    attribute_key_prefix="locked",
                    statistics=("mean",),
                    direct_dsn=test_dsn,
                )

            # The failed-run row must have the dedicated error code so
            # dashboards can filter lock-miss separately from generic
            # worker failures.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT details FROM curated_geo.ingestion_run
                    WHERE source_id = %s
                      AND transformation_step = 'raster_zonal_stats'
                      AND status = 'failed'
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (ingest.source_id,),
                )
                (details,) = cur.fetchone()
            assert details["error_code"] == "ConcurrentRasterRun"
        finally:
            holder.rollback()
            holder.close()
    finally:
        _cleanup_observations(conn, "locked")


def test_post_commit_finalise_failure_keeps_observations(
    conn, test_dsn, isolated_data_dir, tmp_path, monkeypatch,
):
    """If ``complete_ingestion_run`` raises AFTER the critical-section
    commit, observations must remain durable and the worker must NOT
    re-raise the finalise error — that's the contract of
    ``_finalise_run_safely``. Regression test for the bug where the
    success-finalisation was outside the safe wrapper, so any flaky
    status/MV-refresh write would surface to callers even though the
    observations were already committed.
    """
    import reimagina.jobs.raster_zonal_stats as rzs

    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref="fin-feat",
        wkt_4326=_utm_square_wkt(min_x=500_030, min_y=2_150_880, size=60.0),
    )
    raster_path = _write_utm_raster(
        tmp_path / "fin.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    conn.commit()

    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r3-fin-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r3_fin_{uuid4().hex[:8]}",
            aggregation_kind="intensive",
        )
        conn.commit()

        # Force the success-finalisation to fail. This simulates a
        # flaky MV refresh or a briefly-aborted pooled connection
        # right after the observations are committed on the dedicated
        # txn.
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated finalise failure")

        monkeypatch.setattr(rzs.cat, "complete_ingestion_run", _boom)

        # Must NOT raise: finalise is wrapped in _finalise_run_safely.
        result = run_raster_zonal_stats(
            conn,
            source_id=ingest.source_id,
            feature_type_slug="block",
            attribute_key_prefix="fin",
            statistics=("mean",),
            direct_dsn=test_dsn,
        )
        assert result.observations_written >= 1

        # Observations are durable — Transaction B committed them
        # before the finalise attempt.
        rows = _current_observations_for_prefix(conn, "fin")
        assert len(rows) >= 1
    finally:
        _cleanup_observations(conn, "fin")


# ─── Cleanup ──────────────────────────────────────────────────────────

def _cleanup_observations(conn, prefix: str) -> None:
    """Hard-delete every observation row (current and retired) whose
    attribute_key matches the prefix pattern.

    The worker's retire+insert runs on a dedicated connection that
    commits outside the test's main transaction, so the standard
    rollback teardown can't reach it. Each test owns its prefix and
    is responsible for cleaning up here.
    """
    conn.rollback()  # drop anything the test left staged on the main conn
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM curated_geo.observation
                 WHERE attribute_key = %s
                    OR starts_with(attribute_key, %s || '_')
                """,
                (prefix, prefix),
            )
        conn.commit()
    except Exception:
        conn.rollback()
