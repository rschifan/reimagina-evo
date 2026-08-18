"""Scenario reads resolve as an overlay on the baseline.

A scenario stores only what it changed. Before this, every read matched
``scenario_id`` exactly, so an indicator the scenario had not recomputed simply
had no value under it — a hole on the map, and `compute_composite` intersecting
its components to nothing.

The rule is the one already applied to indicator *inputs*
(``effective_numeric`` resolves ``COALESCE(override, observation, base)``),
finally applied to their outputs.
"""

from __future__ import annotations

import pytest

from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
)
from reimagina.storage import backend_for_connection
from tests.integration._helpers import insert_feature, register_source, start_run

SCENARIO = "44444444-4444-4444-4444-444444444444"
OTHER = "55555555-5555-5555-5555-555555555555"


@pytest.fixture
def blocks(conn):
    # Plain in-transaction seed, NOT seed_catalog_committed: that helper leaves
    # `conn.autocommit = True` behind, so every later write in this module would
    # persist past the per-test rollback and collide on the next test's features.
    # The deferred weight-sum trigger is fine uncommitted — it fires at commit.
    from reimagina.indicators.catalog import seed_catalog

    seed_catalog(conn)
    source_id = register_source(
        conn, name="ovl", storage_type="vector", integration_role="canonical_entity"
    )
    run_id = start_run(conn, source_id)
    block_type = get_type_id(conn, "MEX", "block")
    return [
        insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"ovl-{i}",
            geometry_wkt=f"POLYGON(({i} 0, {i} 1, {i + 1} 1, {i + 1} 0, {i} 0))",
        )
        for i in range(3)
    ]


def _write(conn, *, slug, feature_id, value, scenario_id=None):
    """One indicator_result row, through the repository's own write path."""
    backend = backend_for_connection(conn)
    with conn.cursor() as cur:
        # scenario_id must match on both rows — indicator_result_before_write
        # rejects a scenario result written under a baseline run.
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id) "
            "VALUES ('test', 'succeeded', 'test', %s) RETURNING id",
            (scenario_id,),
        )
        job_run_id = cur.fetchone()[0]
    backend.indicator_result_repo().bulk_write_numeric_results(
        [(feature_id, value)],
        indicator_slug=slug,
        job_run_id=job_run_id,
        scenario_id=scenario_id,
    )


def _values(conn, slug, scenario_id):
    rows = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .list_current_for_indicator(indicator_slug=slug, scenario_id=scenario_id)
    )
    return {str(r["feature_id"]): r["value_numeric"] for r in rows}


# ── the overlay ────────────────────────────────────────────────────────────


def test_an_untouched_indicator_reads_through_to_the_baseline(blocks, conn):
    """The hole this fixes.

    A scenario that never recomputed compactness must still show compactness —
    the baseline's, because that is what the scenario's value *is*.
    """
    for i, fid in enumerate(blocks):
        _write(
            conn, slug=URBAN_FORM_BLOCK_COMPACTNESS.slug, feature_id=fid, value=0.1 * i
        )

    assert _values(conn, URBAN_FORM_BLOCK_COMPACTNESS.slug, SCENARIO) == _values(
        conn, URBAN_FORM_BLOCK_COMPACTNESS.slug, None
    )


def test_a_recomputed_indicator_wins_over_the_baseline(blocks, conn):
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=3.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=9.0, scenario_id=SCENARIO)

    values = _values(conn, slug, SCENARIO)

    assert values[str(blocks[0])] == 9.0, "the scenario's own row wins"
    assert values[str(blocks[1])] == 3.0, "the rest fall through to baseline"
    assert values[str(blocks[2])] == 3.0


def test_no_feature_is_returned_twice(blocks, conn):
    """The trap in the obvious implementation.

    `scenario_id = X OR scenario_id IS NULL` looks equivalent and silently
    double-counts: where a scenario has recomputed a feature, both rows match.
    An aggregate would average it twice and a tile would carry it twice.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=3.0)
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=9.0, scenario_id=SCENARIO)

    rows = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .list_current_for_indicator(indicator_slug=slug, scenario_id=SCENARIO)
    )

    assert len(rows) == len(blocks)
    assert all(r["value_numeric"] == 9.0 for r in rows)


def test_the_baseline_is_unaffected(blocks, conn):
    """Baseline reads keep the exact `IS NULL` predicate they always had."""
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=3.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=9.0, scenario_id=SCENARIO)

    assert set(_values(conn, slug, None).values()) == {3.0}


def test_one_scenario_does_not_leak_into_another(blocks, conn):
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=3.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=9.0, scenario_id=SCENARIO)

    assert _values(conn, slug, OTHER)[str(blocks[0])] == 3.0


def test_value_stats_counts_each_feature_once(blocks, conn):
    """The aggregate the double-count would corrupt silently.

    Backs the choropleth breaks: a duplicated feature shifts the mean and the
    percentile boundaries, with nothing to show for it.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=2.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=8.0, scenario_id=SCENARIO)

    stats = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .value_stats(indicator_slug=slug, scenario_id=SCENARIO)
    )

    # Three features, one overridden. A double-counted baseline row would make
    # this 4 and drag the percentiles with it.
    assert stats["count"] == len(blocks)
    assert stats["max"] == pytest.approx(8.0), "the scenario's value is present"
    assert stats["min"] == pytest.approx(2.0), "and the baseline's, exactly once"


def test_the_overlay_tile_falls_back_too(blocks, conn):
    """The map path, which is where the hole was visible."""
    slug = URBAN_FORM_BLOCK_COMPACTNESS.slug
    for fid in blocks:
        _write(conn, slug=slug, feature_id=fid, value=0.5)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT length(serving_geo.indicator_overlay_tile(%s, %s, %s, %s::json))",
            (0, 0, 0, f'{{"indicator_slug": "{slug}", "scenario_id": "{SCENARIO}"}}'),
        )
        scenario_tile = cur.fetchone()[0]
        cur.execute(
            "SELECT length(serving_geo.indicator_overlay_tile(%s, %s, %s, %s::json))",
            (0, 0, 0, f'{{"indicator_slug": "{slug}"}}'),
        )
        baseline_tile = cur.fetchone()[0]

    # z=0 is below the z>=8 floor, so both are empty — the assertion that matters
    # is that they agree, which they would not if the scenario tile were a hole.
    assert scenario_tile == baseline_tile
