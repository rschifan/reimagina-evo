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
    # Keyed by feature, so a feature returned twice would collapse to whichever
    # row the server happened to send last — silently, and differently from one
    # plan to the next. That is not a hypothetical: it is how the valueless-row
    # duplicate below stayed hidden, surfacing only as an intermittent failure
    # of `test_a_valueless_scenario_row_falls_through_to_the_baseline` whenever
    # the planner switched from an index scan to a seq scan. Assert the overlay's
    # one-row-per-pair invariant here rather than let the dict paper over it.
    seen = [str(r["feature_id"]) for r in rows]
    assert len(seen) == len(set(seen)), (
        f"overlay returned a feature more than once: "
        f"{sorted(f for f in set(seen) if seen.count(f) > 1)}"
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


# ── a valueless scenario row must not shadow the baseline ──────────────────


def _write_missing(conn, *, slug, feature_id, scenario_id):
    """A current scenario row carrying no value.

    `write_result(value_numeric=None, quality_flag='missing')` is a supported
    call and 'missing' is a valid quality_flag (r016 CHECK), so these rows exist
    by design — a scenario run that could not compute a feature records one.
    """
    backend = backend_for_connection(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id) "
            "VALUES ('test', 'succeeded', 'test', %s) RETURNING id",
            (scenario_id,),
        )
        job_run_id = cur.fetchone()[0]
    backend.indicator_result_repo().write_result(
        feature_id=feature_id,
        indicator_slug=slug,
        job_run_id=job_run_id,
        value_numeric=None,
        scenario_id=scenario_id,
        quality_flag="missing",
        missing_reason="test",
    )


def test_a_valueless_scenario_row_falls_through_to_the_baseline(blocks, conn):
    """COALESCE skips a NULL override; row existence alone must not shadow.

    `effective_numeric` resolves COALESCE(so.value_numeric, obs.value_numeric,
    base) — a NULL override falls through rather than winning. Keyed on row
    existence instead, a 'missing' scenario row would suppress the baseline and
    then be dropped by the readers' own `value_numeric IS NOT NULL` filters, so
    the feature would vanish rather than fall back.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for b in blocks:
        _write(conn, slug=slug, feature_id=b, value=3.0)
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    values = _values(conn, slug, SCENARIO)
    assert set(values) == {str(b) for b in blocks}, (
        "a valueless scenario row must not remove the feature from the read"
    )
    assert values[str(blocks[0])] == 3.0, "it falls through to the baseline"


def test_value_stats_keeps_a_feature_whose_scenario_row_has_no_value(blocks, conn):
    """The aggregate the hole corrupts silently.

    Without the value test the feature is excluded from both branches, so
    `count` — the denominator — and the p05/p50/p95 choropleth breaks all move.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for i, b in enumerate(blocks):
        _write(conn, slug=slug, feature_id=b, value=float(i + 2))
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    stats = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .value_stats(indicator_slug=slug, scenario_id=SCENARIO)
    )
    assert stats["count"] == len(blocks), "no feature may be dropped"
    assert stats["min"] == pytest.approx(2.0), "the fallen-through baseline counts"


def test_a_valued_scenario_row_still_wins(blocks, conn):
    """The control: the value test must not weaken the overlay itself."""
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for b in blocks:
        _write(conn, slug=slug, feature_id=b, value=3.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=9.0, scenario_id=SCENARIO)

    values = _values(conn, slug, SCENARIO)
    assert values[str(blocks[0])] == 9.0
    assert values[str(blocks[1])] == 3.0


def test_a_valueless_scenario_row_does_not_duplicate_the_feature(blocks, conn):
    """The invariant both the docstring and SCENARIOS_LAYER.md claim: *exactly
    one row survives per (feature, indicator) pair*.

    The value test added to the anti-join stops a valueless scenario row from
    suppressing the baseline — but the `scenario_id = X` branch still matched
    that row unconditionally, so both rows qualified and the pair came back
    twice. The two branches have to be complementary, not just the anti-join
    guarded, or this is the very double-count the anti-join exists to avoid,
    reintroduced one case down.

    Asserted on row count, not on values: a reader that keys by feature (the
    `_values` dict here, `dict(row)` in the API) collapses the duplicate to
    whichever row arrived last, so the defect shows up as a *flake* that tracks
    the query plan rather than as a stable failure.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for b in blocks:
        _write(conn, slug=slug, feature_id=b, value=3.0)
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    rows = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .list_current_for_indicator(indicator_slug=slug, scenario_id=SCENARIO)
    )

    assert len(rows) == len(blocks), (
        "the baseline row and the valueless scenario row both qualified"
    )
    assert sorted(r["value_numeric"] for r in rows) == [3.0, 3.0, 3.0]


def test_a_valueless_scenario_row_survives_with_no_baseline_to_fall_back_to(
    blocks, conn
):
    """The guard on the other side of the fix.

    Making the scenario branch require a value is not enough on its own: where
    the scenario recorded `missing` and the baseline never computed the feature
    at all, requiring a value would drop the row from both branches and the
    feature would vanish — the same hole this module exists to close, in a rarer
    configuration. A baseline read surfaces its own valueless rows, so a
    scenario read must surface the scenario's.
    """
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    rows = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .list_current_for_indicator(indicator_slug=slug, scenario_id=SCENARIO)
    )

    assert [str(r["feature_id"]) for r in rows] == [str(blocks[0])]
    assert rows[0]["value_numeric"] is None
    assert rows[0]["quality_flag"] == "missing"


def test_get_current_falls_through_a_valueless_scenario_row(blocks, conn):
    """`get_current` takes `fetchone()`, so a duplicated pair makes it return
    an arbitrary one of the two rows — reporting "no value" for a feature the
    baseline has a value for, on whichever plan the server picks."""
    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    _write(conn, slug=slug, feature_id=blocks[0], value=3.0)
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    row = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .get_current(feature_id=blocks[0], indicator_slug=slug, scenario_id=SCENARIO)
    )

    assert row is not None
    assert row["value_numeric"] == 3.0, "the baseline value, not the missing row"


def test_the_aoi_aggregate_does_not_double_count_a_valueless_scenario_row(blocks, conn):
    """Where the duplicate stops being cosmetic.

    `fetch_aoi_children` puts the overlay in a `LEFT JOIN … ON`, so a feature
    matching twice is returned twice and its `overlap_measure` weight is counted
    twice in the weighted mean. The AOI summary comes out quietly wrong rather
    than raising — the failure mode
    `test_the_aoi_aggregate_reads_through_to_the_baseline` guards for a *valued*
    scenario row, and which is just as reachable with a valueless one.
    """
    from tests.integration._helpers import insert_project

    from reimagina.data import aoi as aoi_mod

    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for b in blocks:
        _write(conn, slug=slug, feature_id=b, value=3.0)
    _write_missing(conn, slug=slug, feature_id=blocks[0], scenario_id=SCENARIO)

    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=insert_project(conn, name="ovl_missing_aoi_proj"),
        name="ovl_missing_aoi",
        geometry_wkt="POLYGON((-1 -1, -1 5, 5 5, 5 -1, -1 -1))",
    )
    with conn.cursor() as cur:
        for b in blocks:
            cur.execute(
                "INSERT INTO curated_geo.aoi_feature (aoi_id, feature_id, "
                "overlap_basis, overlap_measure, feature_measure, overlap_ratio) "
                "VALUES (%s, %s, 'area', 1.0, 1.0, 1.0)",
                (aoi_id, b),
            )

    rows = (
        backend_for_connection(conn)
        .aoi_summary_repo()
        .fetch_aoi_children(aoi_id=aoi_id, indicator_slug=slug, scenario_id=SCENARIO)
    )

    assert len(rows) == len(blocks), "one row per AOI member, never doubled"
    assert sorted(v for v, _, _ in rows) == [3.0, 3.0, 3.0]


# ── the case this change exists for ────────────────────────────────────────


def test_a_composite_under_a_scenario_writes_rows(blocks, conn):
    """The failure that motivates the overlay, asserted end to end.

    `compute_composite` intersects its components' feature sets. Under
    exact-match reads a scenario that recomputed one input and not the other
    intersected to nothing and wrote **zero rows**, silently. Every other test
    here exercises a read; this one exercises the consumer whose breakage is the
    stated reason for the change.
    """
    from reimagina.indicators.catalog import (
        URBAN_FORM_BLOCK_BUILDING_DENSITY,
        URBAN_FORM_BLOCK_COMPOSITE,
    )
    from reimagina.indicators.composite import compute_composite

    for b in blocks:
        _write(conn, slug=URBAN_FORM_BLOCK_COMPACTNESS.slug, feature_id=b, value=0.5)
        _write(
            conn, slug=URBAN_FORM_BLOCK_BUILDING_DENSITY.slug, feature_id=b, value=100.0
        )
    # The scenario recomputes exactly one of the two inputs, for one feature —
    # the shape a `floors` override produces.
    _write(
        conn,
        slug=URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
        feature_id=blocks[0],
        value=400.0,
        scenario_id=SCENARIO,
    )

    result = compute_composite(
        conn, spec=URBAN_FORM_BLOCK_COMPOSITE, scenario_id=SCENARIO, code_version="test"
    )
    assert result.rows_written == len(blocks), (
        "the composite must cover every feature, not only the recomputed one"
    )

    values = _values(conn, URBAN_FORM_BLOCK_COMPOSITE.slug, SCENARIO)
    # 0.5·norm(compactness) + 0.5·norm(density), bounds 0-1 and 0-500.
    assert values[str(blocks[0])] == pytest.approx(0.5 * 0.5 + 0.5 * (400.0 / 500.0))
    assert values[str(blocks[1])] == pytest.approx(0.5 * 0.5 + 0.5 * (100.0 / 500.0))


def test_the_aoi_aggregate_reads_through_to_the_baseline(blocks, conn):
    """`fetch_aoi_children` puts the overlay in a LEFT JOIN ON rather than a
    WHERE. If a feature matched twice there, its weight would be counted twice
    and the AOI summary would be quietly wrong rather than raising."""
    from tests.integration._helpers import insert_project

    from reimagina.data import aoi as aoi_mod

    slug = URBAN_FORM_BLOCK_PLANNED_FLOORS.slug
    for b in blocks:
        _write(conn, slug=slug, feature_id=b, value=3.0)
    _write(conn, slug=slug, feature_id=blocks[0], value=9.0, scenario_id=SCENARIO)

    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=insert_project(conn, name="ovl_aoi_proj"),
        name="ovl_aoi",
        geometry_wkt="POLYGON((-1 -1, -1 5, 5 5, 5 -1, -1 -1))",
    )
    with conn.cursor() as cur:
        for b in blocks:
            cur.execute(
                "INSERT INTO curated_geo.aoi_feature (aoi_id, feature_id, "
                "overlap_basis, overlap_measure, feature_measure, overlap_ratio) "
                "VALUES (%s, %s, 'area', 1.0, 1.0, 1.0)",
                (aoi_id, b),
            )

    rows = (
        backend_for_connection(conn)
        .aoi_summary_repo()
        .fetch_aoi_children(aoi_id=aoi_id, indicator_slug=slug, scenario_id=SCENARIO)
    )
    assert len(rows) == len(blocks), "one row per AOI member, never doubled"
    assert sorted(v for v, _, _ in rows) == [3.0, 3.0, 9.0]
