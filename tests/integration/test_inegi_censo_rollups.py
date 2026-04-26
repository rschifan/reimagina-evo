"""Integration tests for reimagina.pipeline.inegi_censo_rollups.

End-to-end check against migrated Postgres+PostGIS:

- Insert two block features + one AGEB feature
- Wire feature_overlap rows so the rollup engine knows blocks roll up
  into the AGEB
- Insert block-grain ``censo_2010_*`` observations (numbers chosen so
  the expected sum is unambiguous)
- Call :func:`apply` (seed + run)
- Assert the AGEB rolled-up observations have the right
  ``censo_2010_<col>_ageb`` keys and values

Test isolation: ``apply`` registers and runs recipes that commit. We
clean up explicitly in a ``finally`` block — same shape as the
existing aggregation-recipes integration suite — keyed off the
canonical ``censo_2010`` prefix so we don't strand orphan rows.
"""
from __future__ import annotations

from uuid import uuid4

import os

import pytest

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import observation as obs
from reimagina.pipeline import inegi_censo_rollups


@pytest.fixture
def direct_dsn() -> str:
    """The Postgres rollup worker takes an advisory lock on a dedicated
    connection (separate from the test's transactional ``conn``) — so
    the worker needs a real DSN it can open itself. Reuse the test DB
    URL from the same source the conftest uses."""
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://localhost/reimagina_test",
    )


# ─── Fixtures + helpers ──────────────────────────────────────────────


@pytest.fixture
def census_rollup_fixture(conn):
    """Two blocks rolling up into one AGEB, with hand-picked
    populations so the rollup sums are easy to assert."""
    feat.seed_feature_types(conn)
    block_type = feat.get_type_id(conn, "MEX", "block")
    ageb_type = feat.get_type_id(conn, "MEX", "ageb")

    src = cat.register_source(
        conn,
        name=f"censo-rollup-test-{uuid4().hex[:8]}",
        provider="pytest",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="file:///tmp/test",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn, source_id=src, orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="feature_seed", code_version="test",
    )
    conn.commit()

    block_a = _insert_feature(
        conn, type_id=block_type, source_id=src, ingestion_run_id=run_id,
        ref="0901500010019001",
        wkt="POLYGON((-99.135 19.43, -99.134 19.43, -99.134 19.431, -99.135 19.431, -99.135 19.43))",
    )
    block_b = _insert_feature(
        conn, type_id=block_type, source_id=src, ingestion_run_id=run_id,
        ref="0901500010019002",
        wkt="POLYGON((-99.135 19.431, -99.134 19.431, -99.134 19.432, -99.135 19.432, -99.135 19.431))",
    )
    ageb = _insert_feature(
        conn, type_id=ageb_type, source_id=src, ingestion_run_id=run_id,
        ref="090150001001900",
        wkt="POLYGON((-99.135 19.43, -99.134 19.43, -99.134 19.432, -99.135 19.432, -99.135 19.43))",
    )

    # Both blocks fully inside the AGEB. measure=area is fine for the
    # sum aggregator (it ignores measure for the 'sum' method).
    _seed_overlap(conn, source_id=block_a, target_id=ageb, measure=1.0)
    _seed_overlap(conn, source_id=block_b, target_id=ageb, measure=1.0)

    # Block-grain census observations. Chosen so AGEB sum = 300 for
    # pobtot (100 + 200), 60 for vivtot (20 + 40), etc.
    obs.bulk_upsert_observations(
        conn,
        [
            {"feature_id": block_a, "attribute_key": "censo_2010_pobtot",
             "value_type": "numeric", "value": 100.0},
            {"feature_id": block_b, "attribute_key": "censo_2010_pobtot",
             "value_type": "numeric", "value": 200.0},
            {"feature_id": block_a, "attribute_key": "censo_2010_vivtot",
             "value_type": "numeric", "value": 20.0},
            {"feature_id": block_b, "attribute_key": "censo_2010_vivtot",
             "value_type": "numeric", "value": 40.0},
        ],
        source_id=src, ingestion_run_id=run_id,
    )
    conn.commit()

    yield {
        "block_a": block_a, "block_b": block_b, "ageb": ageb,
        "source_id": src,
    }

    # Cleanup: erase observations + recipes + overlaps + features. The
    # fixtures use the ``censo_2010`` prefix throughout so we sweep by
    # prefix and feature_id.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM curated_geo.observation "
            "WHERE attribute_key LIKE 'censo_2010%'",
        )
        cur.execute(
            "DELETE FROM curated_geo.aggregation_recipe "
            "WHERE attribute_key_prefix LIKE 'censo_2010%' "
            "   OR input_attribute_key_prefix LIKE 'censo_2010%'",
        )
        for fid in (block_a, block_b, ageb):
            cur.execute(
                "DELETE FROM curated_geo.feature_overlap "
                "WHERE source_feature_id = %s OR target_feature_id = %s",
                (fid, fid),
            )
            cur.execute("DELETE FROM curated_geo.feature WHERE id = %s", (fid,))
        cur.execute(
            "DELETE FROM curated_geo.ingestion_run WHERE source_id = %s",
            (src,),
        )
        cur.execute(
            "DELETE FROM curated_geo.data_source_catalog WHERE id = %s",
            (src,),
        )
    conn.commit()


def _insert_feature(conn, *, type_id, source_id, ingestion_run_id, ref, wkt):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (type_id, source_id, source_record_ref, ingestion_run_id,
               dataset_version, geometry, attributes)
            VALUES (%s, %s, %s, %s, 'v1',
                    ST_SetSRID(ST_GeomFromText(%s), 4326), '{}'::jsonb)
            RETURNING id
            """,
            (type_id, source_id, ref, ingestion_run_id, wkt),
        )
        return str(cur.fetchone()[0])


def _seed_overlap(conn, *, source_id, target_id, measure):
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


def _fetch_ageb_observations(conn, ageb_id: str) -> dict[str, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attribute_key, value_numeric
            FROM curated_geo.observation
            WHERE feature_id = %s AND is_current
              AND attribute_key LIKE 'censo_2010%%_ageb'
            """,
            (ageb_id,),
        )
        return {key: float(value) for key, value in cur.fetchall()}


# ─── Tests ────────────────────────────────────────────────────────────


def test_apply_writes_ageb_rollups_with_expected_sums(
    conn, census_rollup_fixture, direct_dsn,
):
    """End-to-end: blocks 100+200 → AGEB pobtot=300; 20+40 → vivtot=60."""
    fixture = census_rollup_fixture
    report = inegi_censo_rollups.apply(
        conn,
        # Restrict to the two columns our fixture seeds; the others
        # would produce empty rollups (no source observations) which
        # is wasted work but not broken.
        columns=("pobtot", "vivtot"),
        target_grains=("ageb",),
        direct_dsn=direct_dsn,
    )

    assert report.recipes_run == 2
    # 1 AGEB × 2 columns = 2 observation rows written.
    assert report.observations_written == 2

    ageb_obs = _fetch_ageb_observations(conn, fixture["ageb"])
    assert ageb_obs["censo_2010_pobtot_ageb"] == 300.0
    assert ageb_obs["censo_2010_vivtot_ageb"] == 60.0


def test_apply_is_idempotent_on_seed(conn, census_rollup_fixture, direct_dsn):
    """Running ``apply`` twice must reuse the existing recipes (UNIQUE
    on (target, attribute_key_prefix) in r020) instead of inserting
    duplicates and erroring out."""
    first = inegi_censo_rollups.apply(
        conn, columns=("pobtot",), target_grains=("ageb",),
        direct_dsn=direct_dsn,
    )
    second = inegi_censo_rollups.apply(
        conn, columns=("pobtot",), target_grains=("ageb",),
        direct_dsn=direct_dsn,
    )
    # First run creates 1 recipe; second run reuses it.
    assert first.seeded_created == 1
    assert first.seeded_reused == 0
    assert second.seeded_created == 0
    assert second.seeded_reused == 1


def test_seed_recipes_creates_distinct_rows_per_grain(
    conn, census_rollup_fixture,
):
    """Same column + different grains must produce distinct recipes
    (different attribute_key_prefix), so the picker can render the
    same demographic at AGEB and municipality independently."""
    result = inegi_censo_rollups.seed_recipes(
        conn, columns=("pobtot",), target_grains=("ageb", "municipality"),
    )
    assert len(result.created_recipe_ids) == 2

    # Verify the recipes target different grains.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT target_feature_type_slug, attribute_key_prefix
            FROM curated_geo.aggregation_recipe
            WHERE id = ANY(%s::uuid[])
            ORDER BY target_feature_type_slug
            """,
            (list(result.created_recipe_ids),),
        )
        rows = cur.fetchall()
    assert {r[0] for r in rows} == {"ageb", "municipality"}
    assert {r[1] for r in rows} == {
        "censo_2010_pobtot_ageb",
        "censo_2010_pobtot_municipality",
    }


def test_run_all_with_no_recipes_returns_empty(conn):
    """No seeded recipes → ``run_all`` must be a no-op rather than raising."""
    # Different columns prefix so the recipes don't exist in the catalog.
    result = inegi_censo_rollups.run_all(
        conn,
        columns=("not_a_real_column_for_test",),
        target_grains=("ageb",),
    )
    assert result == []


def test_apply_lineage_records_input_attribute_key(
    conn, census_rollup_fixture, direct_dsn,
):
    """Each rolled-up observation must carry lineage_ref pointing back
    to the input attribute_key it summed over — operators inspecting
    a value at AGEB grain need a one-hop trail to the block-grain
    column it came from."""
    fixture = census_rollup_fixture
    inegi_censo_rollups.apply(
        conn, columns=("pobtot",), target_grains=("ageb",),
        direct_dsn=direct_dsn,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lineage_ref
            FROM curated_geo.observation
            WHERE feature_id = %s
              AND attribute_key = 'censo_2010_pobtot_ageb'
              AND is_current
            """,
            (fixture["ageb"],),
        )
        row = cur.fetchone()
    assert row is not None
    lineage = row[0]
    assert lineage["source_attribute_key"] == "censo_2010_pobtot"
    assert lineage["input_feature_type"] == "block"
    assert lineage["target_feature_type"] == "ageb"
    assert lineage["method"] == "sum"
    assert lineage["children"] == 2
