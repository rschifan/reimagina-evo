"""Producers publishing versions, and observation of what they cannot produce.

This is the half of the algebra the database cannot derive: every derived version
falls out of the graph, but the leaves have to be told. These tests cover the two
ways a leaf gets told — an ingest completing, and a probe noticing.
"""

from __future__ import annotations

import pytest

from reimagina.assets.observe import observe_assets
from reimagina.assets.publish import (
    asset_key_for_feature_type,
    publish_version,
    retire_version,
)
from reimagina.assets.registry import seed_assets
from reimagina.data import catalog as cat
from reimagina.data.feature import get_type_id
from tests.integration._helpers import register_source


@pytest.fixture
def seeded(conn):
    seed_assets(conn)
    return conn


def _current(conn, asset_key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT av.version, av.sequence, av.row_count "
            "FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = %s AND av.is_current",
            (asset_key,),
        )
        return cur.fetchone()


def _state(conn, asset_key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM serving_geo.asset_freshness_v "
            "WHERE asset_key = %s AND partition_key = ''",
            (asset_key,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _start_run(conn, *, name="assets-test"):
    source_id = register_source(
        conn, name=name, storage_type="vector", integration_role="canonical_entity"
    )
    return cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="test",
        orchestrator_run_id=f"{name}-1",
        transformation_step="silver_to_curated",
        code_version="test",
    )


# ── resolution ─────────────────────────────────────────────────────────────


def test_a_feature_type_resolves_to_its_asset(seeded, conn):
    """Resolved through the FK, not by rebuilding a context:type_slug string."""
    assert (
        asset_key_for_feature_type(conn, get_type_id(conn, "global", "building"))
        == "global.building"
    )


def test_an_unregistered_feature_type_resolves_to_nothing(seeded, conn):
    """A layer the graph has not adopted must not break an ingest."""
    assert asset_key_for_feature_type(conn, get_type_id(conn, "global", "tree")) is None


# ── publishing on ingest ───────────────────────────────────────────────────


def test_a_successful_ingest_publishes_a_version(seeded, conn):
    run_id = _start_run(conn)
    cat.complete_ingestion_run(
        conn,
        run_id,
        status="succeeded",
        details={"records_written": 12},
        asset_key="global.building",
        rows_written=12,
        dataset_version="2026-04-15.0",
    )
    version, sequence, row_count = _current(conn, "global.building")
    assert version == str(run_id)
    assert sequence == 1
    assert row_count == 12


def test_a_zero_row_ingest_publishes_nothing(seeded, conn):
    """Vector loads are ON CONFLICT DO NOTHING.

    Re-running an ingest at the same dataset_version writes no rows and changes
    no content, so bumping the version would stale every downstream indicator to
    recompute identical numbers.
    """
    run_id = _start_run(conn)
    cat.complete_ingestion_run(
        conn,
        run_id,
        status="succeeded",
        details={"records_written": 0},
        asset_key="global.building",
        rows_written=0,
    )
    assert _current(conn, "global.building") is None


def test_a_failed_ingest_publishes_nothing(seeded, conn):
    run_id = _start_run(conn)
    cat.complete_ingestion_run(
        conn,
        run_id,
        status="failed",
        details={"error_type": "Boom"},
        asset_key="global.building",
        rows_written=99,
    )
    assert _current(conn, "global.building") is None


def test_a_second_ingest_supersedes_the_first(seeded, conn):
    first = _start_run(conn, name="assets-test-a")
    cat.complete_ingestion_run(
        conn, first, status="succeeded", asset_key="global.building", rows_written=1
    )
    second = _start_run(conn, name="assets-test-b")
    cat.complete_ingestion_run(
        conn, second, status="succeeded", asset_key="global.building", rows_written=1
    )
    version, sequence, _ = _current(conn, "global.building")
    assert version == str(second)
    assert sequence == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = 'global.building'"
        )
        assert cur.fetchone()[0] == 2, "history is append-only"


def test_publishing_the_same_version_twice_is_a_no_op(seeded, conn):
    """A retried ingest must not manufacture a change."""
    publish_version(conn, asset_key="global.building", version="v1")
    publish_version(conn, asset_key="global.building", version="v1")
    _, sequence, _ = _current(conn, "global.building")
    assert sequence == 1


def test_an_unregistered_asset_key_publishes_nothing(seeded, conn):
    assert publish_version(conn, asset_key="nope.nothing", version="v1") is None


def test_ingest_stamps_ingested_at(seeded, conn):
    """The column has existed since r002 and nothing ever wrote it."""
    run_id = _start_run(conn)
    cat.complete_ingestion_run(
        conn, run_id, status="succeeded", asset_key="global.building", rows_written=3
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.ingested_at IS NOT NULL FROM curated_geo.data_source_catalog s "
            "JOIN curated_geo.ingestion_run r ON r.source_id = s.id WHERE r.id = %s",
            (run_id,),
        )
        assert cur.fetchone()[0] is True


def _materialize_every_indicator(conn):
    """Record a materialization at each derived asset's current expected version.

    Cheaper than running the indicators and exactly as good for these tests: the
    point is a graph where nothing is stale, so that whatever the test does next
    is the only reason anything becomes stale.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization (asset_id, version)
            SELECT e.asset_id, e.expected_version
            FROM serving_geo.asset_expected_version_v e
            JOIN curated_geo.asset a ON a.id = e.asset_id
            WHERE e.partition_key = '' AND a.kind = 'derived'
            """
        )


def test_publishing_buildings_stales_only_what_reads_them(seeded, conn):
    """The user's question, end to end on the shipped graph.

    Reloading buildings must stale density and the composite, and must leave
    compactness alone — it reads blocks only.
    """
    _materialize_every_indicator(conn)
    assert _state(conn, "indicator.urban_form_block_building_density") == "fresh"

    run_id = _start_run(conn)
    cat.complete_ingestion_run(
        conn, run_id, status="succeeded", asset_key="global.building", rows_written=5
    )

    assert _state(conn, "indicator.urban_form_block_building_density") == "stale"
    assert _state(conn, "indicator.urban_form_block_composite") == "stale"
    assert _state(conn, "indicator.urban_form_block_compactness") == "fresh"


# ── observation ────────────────────────────────────────────────────────────


def _add_road_edge(conn, osm_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO curated_geo.road_node (id, geometry) VALUES "
            "(%s, ST_SetSRID(ST_MakePoint(0, 0), 4326)), "
            "(%s, ST_SetSRID(ST_MakePoint(1, 1), 4326)) "
            "ON CONFLICT DO NOTHING",
            (osm_id, osm_id + 1),
        )
        cur.execute(
            "INSERT INTO curated_geo.road_edge "
            "(osm_id, source, target, cost, reverse_cost, geometry) VALUES "
            "(%s, %s, %s, 1.0, 1.0, "
            "ST_SetSRID(ST_MakeLine(ST_MakePoint(0,0), ST_MakePoint(1,1)), 4326))",
            (osm_id, osm_id, osm_id + 1),
        )


def test_observing_an_external_asset_publishes_its_content_signal(seeded, conn):
    """road_edge is loaded from Overpass with no ingestion_run at all.

    Nothing in the ingest path can publish for it, so without observation it
    would report itself fresh forever — the failure mode that makes a staleness
    system worth less than none, because it is believed.
    """
    _add_road_edge(conn, 1000)
    results = {o.asset_key: o for o in observe_assets(conn)}
    assert results["global.road_network"].changed is True
    assert _current(conn, "global.road_network") is not None


def test_observing_twice_without_a_change_publishes_nothing(seeded, conn):
    _add_road_edge(conn, 2000)
    observe_assets(conn)
    _, after_first, _ = _current(conn, "global.road_network")

    again = {o.asset_key: o for o in observe_assets(conn)}

    assert again["global.road_network"].changed is False
    # Relative, not absolute: the asset may already carry a version history from
    # an earlier observation, and what matters is that this one added nothing.
    _, after_second, _ = _current(conn, "global.road_network")
    assert after_second == after_first


def test_a_road_change_stales_only_the_network_indicators(seeded, conn):
    """The discrimination `reads=("geometry",)` could not express.

    The three straight-line accessibility indicators consume POIs, not the
    network, so editing the road graph must leave them alone.
    """
    _add_road_edge(conn, 3000)
    observe_assets(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization (asset_id, version)
            SELECT e.asset_id, e.expected_version
            FROM serving_geo.asset_expected_version_v e
            JOIN curated_geo.asset a ON a.id = e.asset_id
            WHERE e.partition_key = '' AND a.kind = 'derived'
            """
        )
    _add_road_edge(conn, 3001)
    observe_assets(conn)

    for category in ("school", "health", "park"):
        assert (
            _state(conn, f"indicator.accessibility_block_{category}_network_m")
            == "stale"
        )


def test_a_failing_probe_is_reported_not_raised(seeded, conn):
    """One misconfigured asset must not stop the rest being observed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset SET probe_sql = 'SELECT * FROM nope.nothing' "
            "WHERE asset_key = 'global.road_network'"
        )
    results = {o.asset_key: o for o in observe_assets(conn)}
    assert results["global.road_network"].error is not None
    assert results["global.road_network"].changed is False


# ── review findings ────────────────────────────────────────────────────────


def test_a_failing_probe_keeps_earlier_publishes(seeded, conn):
    """A bad probe must not take down the rest of the sweep's work.

    `conn.rollback()` would discard versions published for assets probed
    earlier, while still reporting them as changed — losing a publish and
    announcing it, which is the failure this design exists to remove.
    """
    _add_road_edge(conn, 4000)
    with conn.cursor() as cur:
        # A second observable asset that is probed after road_network
        # alphabetically, so the failure lands *after* a successful publish.
        cur.execute(
            "INSERT INTO curated_geo.asset "
            "(asset_key, kind, backing_kind, version_strategy, probe_sql) VALUES "
            "('zz.broken', 'external', 'relation', 'observed', "
            "'SELECT * FROM nope.nothing')"
        )

    results = {o.asset_key: o for o in observe_assets(conn)}

    assert results["zz.broken"].error is not None
    assert results["global.road_network"].changed is True
    # The publish it reported must still be there.
    assert _current(conn, "global.road_network") is not None


def test_publishing_after_a_retire_without_replace(seeded, conn):
    """Zero current rows is a legal state — the index is partial.

    Deriving the next sequence from the live row would restart at 1 and collide
    with retired history, failing the ingest that triggered it.
    """
    publish_version(conn, asset_key="global.building", version="v1")
    publish_version(conn, asset_key="global.building", version="v2")
    assert retire_version(conn, asset_key="global.building") == "v2"

    publish_version(conn, asset_key="global.building", version="v3")

    _, sequence, _ = _current(conn, "global.building")
    assert sequence == 3


def test_a_derived_asset_cannot_carry_a_probe(seeded, conn):
    """The CHECK was vacuous for NULL version_strategy: NULL = x is NULL."""
    import pytest as _pytest

    with _pytest.raises(Exception, match="asset_probe_shape"):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO curated_geo.asset "
                "(asset_key, kind, backing_kind, producer, code_version, probe_sql) "
                "VALUES ('x.derived', 'derived', 'indicator', 'p', '1', 'SELECT 1')"
            )


def test_batched_overrides_publish_once(seeded, conn):
    """Per-edit publishing digests the whole scenario each time — quadratic."""
    from reimagina.scenarios.overrides import apply_overrides

    from tests.integration._helpers import insert_feature, register_source, start_run

    source_id = register_source(
        conn,
        name="batch-src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    block_type = get_type_id(conn, "MEX", "block")
    features = [
        insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"blk-{i}",
            geometry_wkt=f"POLYGON(({i} 0, {i} 1, {i + 1} 1, {i + 1} 0, {i} 0))",
        )
        for i in range(3)
    ]

    applied = apply_overrides(
        conn,
        scenario_id="33333333-3333-3333-3333-333333333333",
        edits=[
            {"feature_id": f, "attribute_key": "floors", "value_numeric": 9.0}
            for f in features
        ],
    )

    assert applied == 3
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = 'scenario.override.floors'"
        )
        assert cur.fetchone()[0] == 1, "three edits, one published version"


# ── retiring a version ──────────────────────────────────────────────────────


def test_retiring_leaves_the_asset_absent_not_fresh(seeded, conn):
    """The state `publish_version` alone cannot express.

    A layer that was truncated or dropped has no new version to publish. Saying
    so with a sentinel string would leave it reading `fresh` at a version whose
    whole meaning is that it is gone.
    """
    publish_version(conn, asset_key="global.building", version="v1")
    assert _state(conn, "global.building") == "fresh"

    retire_version(conn, asset_key="global.building")

    assert _state(conn, "global.building") == "absent"
    assert _current(conn, "global.building") is None


def test_retiring_stales_the_consumers_of_what_was_retired(seeded, conn):
    """Retiring is a real change, not a hole — downstream must notice it."""
    publish_version(conn, asset_key="global.building", version="v1")
    _materialize_every_indicator(conn)
    assert _state(conn, "indicator.urban_form_block_building_density") == "fresh"

    retire_version(conn, asset_key="global.building")

    assert _state(conn, "indicator.urban_form_block_building_density") == "stale"
    # ...and only its consumers. Compactness reads blocks, not buildings.
    assert _state(conn, "indicator.urban_form_block_compactness") == "fresh"


def test_retiring_is_stable_so_the_consumer_does_not_oscillate(seeded, conn):
    """`missing` is a fixed component, not a timestamp.

    A retired asset that hashed differently on each read would make its
    consumers permanently stale-on-arrival: recompute, materialize, and find
    them stale again immediately.
    """
    publish_version(conn, asset_key="global.building", version="v1")
    retire_version(conn, asset_key="global.building")
    _materialize_every_indicator(conn)

    assert _state(conn, "indicator.urban_form_block_building_density") == "fresh"


def test_retiring_what_is_not_there_is_a_state_not_an_error(seeded, conn):
    assert retire_version(conn, asset_key="global.building") is None
    assert retire_version(conn, asset_key="does.not.exist") is None


def test_a_probe_that_finds_nothing_retires_instead_of_publishing_a_sentinel(
    seeded, conn
):
    """`road_edge` emptied out: observed, recorded, and honestly absent."""
    _add_road_edge(conn, osm_id=1)
    observe_assets(conn, asset_keys=["global.road_network"])
    assert _state(conn, "global.road_network") == "fresh"

    with conn.cursor() as cur:
        cur.execute("DELETE FROM curated_geo.road_edge")
        # The shipped probe counts, so an empty table still answers '0:'. Force
        # the no-signal case the way a dropped relation would.
        cur.execute(
            "UPDATE curated_geo.asset SET probe_sql = 'SELECT max(osm_id) "
            "FROM curated_geo.road_edge' WHERE asset_key = 'global.road_network'"
        )

    result = observe_assets(conn, asset_keys=["global.road_network"])[0]

    assert result.version is None
    assert result.error is None, "a probe that ran and found nothing is not a failure"
    assert result.changed is True
    assert _state(conn, "global.road_network") == "absent"


def test_deleting_a_scenario_last_override_retires_that_attribute(seeded, conn):
    """The reconcile half of `publish_scenario_version`.

    Removing the last override for an attribute is a real change: the scenario
    no longer edits it. Leaving the last digest current would keep the scenario
    reading a what-if it does not have.
    """
    from reimagina.scenarios.overrides import apply_override, publish_scenario_version

    scenario = "99999999-9999-9999-9999-999999999999"
    from tests.integration._helpers import insert_feature, start_run

    source_id = register_source(
        conn,
        name="retire-src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    feature = insert_feature(
        conn,
        type_id=get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="retire-blk",
        geometry_wkt="POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))",
    )
    apply_override(
        conn,
        scenario_id=scenario,
        feature_id=feature,
        attribute_key="floors",
        value_numeric=9.0,
    )
    assert _current_partition(conn, "scenario.override.floors", scenario) is not None

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM curated_geo.scenario_override WHERE scenario_id = %s",
            (scenario,),
        )
    # No `attributes` argument: the reconcile has to notice on its own.
    assert publish_scenario_version(conn, scenario) == {"floors": None}

    assert _current_partition(conn, "scenario.override.floors", scenario) is None


def _current_partition(conn, asset_key, scenario_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT av.version FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = %s AND av.partition_key = %s AND av.is_current",
            (asset_key, f"scenario={scenario_id}"),
        )
        row = cur.fetchone()
        return row[0] if row else None
