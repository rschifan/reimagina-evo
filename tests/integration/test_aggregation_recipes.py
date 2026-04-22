"""Integration tests for the r020 aggregation-recipe dispatcher.

Exercises the two arms of :func:`reimagina.jobs.derivations.run_recipe`
against the migrated Postgres test DB:

- **raster arm** — a recipe with ``input_kind='raster'`` invokes the
  R3 zonal-stats worker with method-derived statistics (``method='sum'``
  → ``stats=('sum',)``), producing the same SCD retire+upsert result
  an imperative ``zonal-stats`` call would.
- **observation arm** — a recipe with ``input_kind='observation'``
  reads current observations at a finer grain, joins
  ``feature_overlap``, aggregates via the single shared
  :func:`reimagina.jobs.aggregation.aggregate` function, and writes
  observations at the target grain through the same retire+upsert
  contract as the raster arm.

What these tests catch that existing R3 tests don't:

- The XOR CHECK on ``aggregation_recipe`` actually rejects malformed
  rows at the DB, not just in the CLI.
- ``run_recipe`` dispatches correctly by ``input_kind``.
- An observation→observation rollup produces one output observation
  per ``(target_feature, source_attribute_key)`` pair — i.e. a prefix
  family like ``pop_sum`` + ``pop_count`` rolls up each suffix
  independently.
- The lineage_ref on rolled-up observations carries the source
  attribute_key so operators can trace where the value came from.
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

from reimagina.data.feature import seed_feature_types
from reimagina.jobs.derivations import (
    DisabledRecipe,
    UnknownRecipe,
    run_all_for_raster,
    run_recipe,
)
from reimagina.pipeline.raster import ingest_raster_source
from reimagina.storage import backend_for_connection


# ─── Fixtures (mirror the raster-zonal-stats suite) ──────────────────


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


# ─── Helpers ─────────────────────────────────────────────────────────


def _write_utm_raster(path: Path, data: np.ndarray) -> Path:
    with rasterio.open(
        path, "w",
        driver="GTiff", height=data.shape[0], width=data.shape[1], count=1,
        dtype="float32", crs="EPSG:32614",
        transform=from_origin(500_000.0, 2_151_000.0, 30.0, 30.0),
        nodata=-999.0,
    ) as dst:
        dst.write(data.astype("float32"), 1)
    return path


def _utm_square_wkt(min_x: float, min_y: float, size: float) -> str:
    from pyproj import Transformer
    trans = Transformer.from_crs("EPSG:32614", "EPSG:4326", always_xy=True)
    pts = [
        trans.transform(min_x, min_y),
        trans.transform(min_x + size, min_y),
        trans.transform(min_x + size, min_y + size),
        trans.transform(min_x, min_y + size),
        trans.transform(min_x, min_y),
    ]
    return f"POLYGON(({', '.join(f'{x} {y}' for x, y in pts)}))"


def _seed_feature(conn, *, type_id, source_id, run_id, ref, wkt_4326):
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
    from reimagina.data import catalog as cat
    source_id = cat.register_source(
        conn,
        name=f"r20-test-features-{uuid4()}",
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


def _seed_overlap(conn, *, source_id: str, target_id: str, measure: float):
    """Insert one feature_overlap row. The test bypasses
    ``compute_feature_overlaps`` so it doesn't have to ship real geometries
    with deterministic m² overlaps — measure is supplied directly."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature_overlap
              (source_feature_id, target_feature_id, overlap_basis,
               overlap_measure, feature_measure, overlap_ratio)
            VALUES (%s, %s, 'area', %s, %s, 1.0)
            """,
            (source_id, target_id, measure, measure),
        )


def _current_observations_for_prefix(conn, prefix: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT feature_id::text, attribute_key, value_numeric, lineage_ref
            FROM curated_geo.observation
            WHERE is_current
              AND (attribute_key = %s OR starts_with(attribute_key, %s || '_'))
            ORDER BY feature_id, attribute_key
            """,
            (prefix, prefix),
        )
        return cur.fetchall()


def _cleanup(
    conn,
    *prefixes: str,
    source_ids: list[str] | None = None,
    feature_ids: list[str] | None = None,
    recipe_ids: list[str] | None = None,
) -> None:
    """Erase every row a test created in the shared test DB.

    Tests in this file stitch together features + overlaps + sources +
    observations + recipes — cleaning only the observations would leave
    dangling rows that other tests (e.g. ``test_data_layer_contracts``)
    fail on via count assertions. Always pass every id the test
    created so this function can walk the FK tree in reverse order.
    """
    with conn.cursor() as cur:
        for prefix in prefixes:
            cur.execute(
                """
                DELETE FROM curated_geo.observation
                WHERE attribute_key = %s
                   OR starts_with(attribute_key, %s || '_')
                """,
                (prefix, prefix),
            )
        for rid in recipe_ids or []:
            cur.execute(
                "DELETE FROM curated_geo.aggregation_recipe WHERE id = %s",
                (rid,),
            )
        for fid in feature_ids or []:
            cur.execute(
                "DELETE FROM curated_geo.feature_overlap "
                "WHERE source_feature_id = %s OR target_feature_id = %s",
                (fid, fid),
            )
            cur.execute("DELETE FROM curated_geo.feature WHERE id = %s", (fid,))
        for sid in source_ids or []:
            cur.execute(
                "DELETE FROM curated_geo.raster_layer WHERE source_id = %s",
                (sid,),
            )
            cur.execute(
                "DELETE FROM curated_geo.file_asset WHERE source_id = %s",
                (sid,),
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
                (sid,),
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
                (sid,),
            )
    conn.commit()


# ─── Tests ───────────────────────────────────────────────────────────


def test_raster_recipe_dispatches_to_zonal_stats_worker(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """input_kind='raster' + method='sum' produces exactly one stat
    suffix (``<prefix>_sum``) and observation rows land at the target
    feature grain."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    feat_a = _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref=f"r20-rast-{uuid4()}",
        wkt_4326=_utm_square_wkt(500_030, 2_150_880, 60.0),
    )
    conn.commit()

    raster_path = _write_utm_raster(
        tmp_path / "rast.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    prefix = f"r20_rast_{uuid4().hex[:8]}"
    ingest = None
    recipe_id = None
    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r20-rast-src-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r20_rast_layer_{uuid4().hex[:8]}",
            aggregation_kind="extensive",
        )
        conn.commit()

        layer = backend.raster_layer_repo().get_by_source(ingest.source_id)
        recipe_id = backend.aggregation_recipe_repo().register_recipe(
            input_kind="raster",
            target_feature_type_slug="block",
            attribute_key_prefix=prefix,
            method="sum",
            input_raster_layer_id=layer["id"],
            coverage_threshold=0.0,
        )
        conn.commit()

        result = run_recipe(conn, recipe_id=recipe_id, direct_dsn=test_dsn)
        assert result.input_kind == "raster"
        assert result.observations_written >= 1

        rows = _current_observations_for_prefix(conn, prefix)
        keys = {k for _, k, _, _ in rows}
        assert keys == {f"{prefix}_sum"}, (
            f"method='sum' should produce exactly '<prefix>_sum'; got {keys!r}"
        )
    finally:
        _cleanup(
            conn, prefix,
            recipe_ids=[recipe_id] if recipe_id else [],
            feature_ids=[feat_a],
            source_ids=[ingest.source_id, feat_source_id] if ingest else [feat_source_id],
        )


def test_observation_recipe_rolls_up_to_coarser_grain(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """Seed block-grain observations, declare an observation recipe
    targeting AGEB with method='sum', verify the rollup produced one
    output row per target feature and carried the source attribute_key
    suffix through."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    block_type = backend.feature_repo().get_type_id("MEX", "block")
    ageb_type = backend.feature_repo().get_type_id("MEX", "ageb")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    ageb_id = _seed_feature(
        conn, type_id=ageb_type, source_id=feat_source_id, run_id=feat_run_id,
        ref=f"r20-ageb-{uuid4()}",
        wkt_4326=_utm_square_wkt(500_000, 2_150_800, 200.0),
    )
    block_a = _seed_feature(
        conn, type_id=block_type, source_id=feat_source_id, run_id=feat_run_id,
        ref=f"r20-block-a-{uuid4()}",
        wkt_4326=_utm_square_wkt(500_000, 2_150_800, 60.0),
    )
    block_b = _seed_feature(
        conn, type_id=block_type, source_id=feat_source_id, run_id=feat_run_id,
        ref=f"r20-block-b-{uuid4()}",
        wkt_4326=_utm_square_wkt(500_100, 2_150_800, 60.0),
    )

    _seed_overlap(conn, source_id=block_a, target_id=ageb_id, measure=3600.0)
    _seed_overlap(conn, source_id=block_b, target_id=ageb_id, measure=3600.0)

    # Seed block-grain observations: two suffixes so the rollup exercises
    # the "one recipe produces many output keys" path.
    input_prefix = f"r20_in_{uuid4().hex[:8]}"
    output_prefix = f"r20_out_{uuid4().hex[:8]}"
    from reimagina.data import catalog as cat
    obs_source = cat.register_source(
        conn,
        name=f"r20-obs-{uuid4()}",
        provider="pytest",
        storage_type="raster",
        integration_role="enrichment",
        storage_uri="file://fake",
        version="v1",
    )
    obs_run = cat.start_ingestion_run(
        conn,
        source_id=obs_source,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="seed",
        code_version="test",
    )
    conn.commit()

    with conn.cursor() as cur:
        for feature_id, sum_val, count_val in (
            (block_a, 100.0, 16.0),
            (block_b, 250.0, 16.0),
        ):
            for suffix, val in (("sum", sum_val), ("count", count_val)):
                cur.execute(
                    """
                    INSERT INTO curated_geo.observation
                      (feature_id, attribute_key, value_type, value_numeric,
                       source_id, ingestion_run_id, is_current)
                    VALUES (%s, %s, 'numeric', %s, %s, %s, true)
                    """,
                    (feature_id, f"{input_prefix}_{suffix}", val, obs_source, obs_run),
                )
    conn.commit()

    recipe_id = None
    rollup_source_id = None
    try:
        recipe_id = backend.aggregation_recipe_repo().register_recipe(
            input_kind="observation",
            target_feature_type_slug="ageb",
            attribute_key_prefix=output_prefix,
            method="sum",
            input_attribute_key_prefix=input_prefix,
            input_feature_type_slug="block",
        )
        conn.commit()

        result = run_recipe(conn, recipe_id=recipe_id, direct_dsn=test_dsn)
        assert result.input_kind == "observation"
        rollup_source_id = result.inner.source_id

        rows = _current_observations_for_prefix(conn, output_prefix)
        # Expect two rows at the AGEB: <output>_sum and <output>_count —
        # one per source suffix, each aggregated independently.
        by_key = {k: (fid, v) for fid, k, v, _ in rows}
        assert set(by_key) == {f"{output_prefix}_sum", f"{output_prefix}_count"}
        assert by_key[f"{output_prefix}_sum"][0] == ageb_id
        assert by_key[f"{output_prefix}_sum"][1] == pytest.approx(350.0)   # 100 + 250
        assert by_key[f"{output_prefix}_count"][1] == pytest.approx(32.0)  # 16 + 16

        # Lineage carries the source attribute_key so operators can trace
        # which block-side family fed this AGEB row.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attribute_key, lineage_ref FROM curated_geo.observation "
                "WHERE is_current AND attribute_key = %s",
                (f"{output_prefix}_sum",),
            )
            (_, lineage) = cur.fetchone()
        assert lineage["source_attribute_key"] == f"{input_prefix}_sum"
        assert lineage["method"] == "sum"
        assert lineage["children"] == 2
    finally:
        _cleanup(
            conn, output_prefix, input_prefix,
            recipe_ids=[recipe_id] if recipe_id else [],
            feature_ids=[block_a, block_b, ageb_id],
            source_ids=(
                [obs_source, feat_source_id]
                + ([rollup_source_id] if rollup_source_id else [])
            ),
        )


def test_db_xor_check_rejects_malformed_recipe(conn, test_dsn):
    """The aggregation_recipe_input_shape CHECK must reject rows where
    input_kind='raster' but input_raster_layer_id is NULL (or vice versa
    for observation). Catches the failure mode the CLI can't: someone
    INSERTing directly via SQL."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO curated_geo.aggregation_recipe
                  (input_kind, target_feature_type_slug, attribute_key_prefix, method)
                VALUES ('raster', 'block', 'xor_test', 'sum')
                """,
            )
    conn.rollback()


def test_run_recipe_raises_on_unknown_id(conn):
    """run_recipe must raise UnknownRecipe — not a SQL or attribute error
    — when the id doesn't resolve. The dispatcher's public contract
    guarantees this shape so callers (CLI, future orchestrator) can
    handle it uniformly."""
    with pytest.raises(UnknownRecipe):
        run_recipe(conn, recipe_id="00000000-0000-0000-0000-000000000000")


def test_run_recipe_refuses_disabled_recipe(conn, test_dsn):
    """Disabled recipes never run. Operators disable a recipe when the
    output is suspect; the dispatcher must honour that instead of
    overwriting observations."""
    backend = backend_for_connection(conn)
    prefix = f"r20_disabled_{uuid4().hex[:8]}"
    recipe_id = backend.aggregation_recipe_repo().register_recipe(
        input_kind="observation",
        target_feature_type_slug="ageb",
        attribute_key_prefix=prefix,
        method="sum",
        input_attribute_key_prefix=prefix + "_src",
        input_feature_type_slug="block",
        enabled=False,
    )
    conn.commit()
    try:
        with pytest.raises(DisabledRecipe):
            run_recipe(conn, recipe_id=recipe_id)
    finally:
        backend.aggregation_recipe_repo().delete_recipe(recipe_id)
        conn.commit()


def test_run_all_for_raster_runs_every_enabled_recipe(
    conn, test_dsn, isolated_data_dir, tmp_path,
):
    """``run_all_for_raster`` is the ingest-time / CLI shortcut that
    materialises every enabled recipe whose input is the given layer.
    Disabled recipes on the same layer must be skipped."""
    seed_feature_types(conn)
    backend = backend_for_connection(conn)
    type_id = backend.feature_repo().get_type_id("MEX", "block")

    feat_source_id, feat_run_id = _seed_feature_source(conn)
    feat_a = _seed_feature(
        conn, type_id=type_id, source_id=feat_source_id, run_id=feat_run_id,
        ref=f"r20-all-{uuid4()}",
        wkt_4326=_utm_square_wkt(500_030, 2_150_880, 60.0),
    )
    conn.commit()

    raster_path = _write_utm_raster(
        tmp_path / "all.tif",
        np.arange(100, dtype="float32").reshape(10, 10),
    )
    prefix_on = f"r20_on_{uuid4().hex[:8]}"
    prefix_off = f"r20_off_{uuid4().hex[:8]}"
    ingest = None
    on_id = off_id = None
    try:
        ingest = ingest_raster_source(
            conn,
            raw_path=raster_path,
            source_name=f"r20-all-src-{uuid4()}",
            provider="pytest", version="v1",
            layer_slug=f"r20_all_layer_{uuid4().hex[:8]}",
            aggregation_kind="extensive",
        )
        conn.commit()

        layer = backend.raster_layer_repo().get_by_source(ingest.source_id)
        repo = backend.aggregation_recipe_repo()
        on_id = repo.register_recipe(
            input_kind="raster",
            target_feature_type_slug="block",
            attribute_key_prefix=prefix_on,
            method="sum",
            input_raster_layer_id=layer["id"],
            coverage_threshold=0.0,
        )
        off_id = repo.register_recipe(
            input_kind="raster",
            target_feature_type_slug="block",
            attribute_key_prefix=prefix_off,
            method="sum",
            input_raster_layer_id=layer["id"],
            coverage_threshold=0.0,
            enabled=False,
        )
        conn.commit()

        results = run_all_for_raster(
            conn, raster_layer_id=layer["id"], direct_dsn=test_dsn,
        )
        assert len(results) == 1, "disabled recipe must be skipped"
        assert results[0].attribute_key_prefix == prefix_on
    finally:
        _cleanup(
            conn, prefix_on, prefix_off,
            recipe_ids=[i for i in (on_id, off_id) if i],
            feature_ids=[feat_a],
            source_ids=[ingest.source_id, feat_source_id] if ingest else [feat_source_id],
        )
