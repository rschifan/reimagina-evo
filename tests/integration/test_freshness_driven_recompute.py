"""Freshness driving the dirty set, instead of a guessed attribute name.

This is the payoff of the asset graph and the point where it changes behaviour:
a submit that names nothing now recomputes what is genuinely out of date, and an
ingest no longer has to invent an attribute that stands in for the layer it just
wrote.
"""

from __future__ import annotations

import pytest

from reimagina.assets.publish import publish_version
from reimagina.assets.registry import seed_assets
from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.orchestration.plan import plan_run


def _materialize_everything(conn):
    """Bring every indicator to its expected version — the 'all fresh' state."""
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


def _plan(conn, **kwargs):
    return plan_run(
        conn,
        changed_attributes=kwargs.pop("changed_attributes", ()),
        edited_features=kwargs.pop("edited_features", ()),
        registry=SHIPPED_INDICATORS,
        **kwargs,
    )


def _slugs(plan):
    return [u.indicator for u in plan.units]


@pytest.fixture
def graph(conn):
    seed_assets(conn)
    return conn


def test_nothing_stale_is_a_noop(graph, conn):
    _materialize_everything(conn)
    assert _plan(conn).is_noop


def test_absent_results_do_not_count_as_dirty(graph, conn):
    """The safety rule.

    On a database where nothing has been computed every indicator is absent. If
    absence counted, an empty submit would mean "compute the entire platform" —
    a very expensive way to discover the default was wrong.
    """
    assert _plan(conn).is_noop


def test_reloading_a_layer_recomputes_exactly_its_consumers(graph, conn):
    """The whole point, end to end.

    Nobody names an attribute. The building layer publishes a version, and the
    two indicators that read buildings say they are out of date.
    """
    _materialize_everything(conn)
    publish_version(conn, asset_key="global.building", version="release-2")

    slugs = _slugs(_plan(conn))

    assert set(slugs) == {
        "urban_form_block_building_density",
        "urban_form_block_composite",
    }
    # Dependency order, not merely membership: the composite reads the density
    # it is queued behind.
    assert slugs.index("urban_form_block_building_density") < slugs.index(
        "urban_form_block_composite"
    )


def test_compactness_survives_a_building_reload(graph, conn):
    """What the old default got wrong.

    submit_after_ingest passed ("geometry",) because writing footprints changes
    geometry — true, but it named an attribute three indicators read, so
    reloading buildings also recomputed compactness, which never looks at a
    building.
    """
    _materialize_everything(conn)
    publish_version(conn, asset_key="global.building", version="release-2")

    assert "urban_form_block_compactness" not in _slugs(_plan(conn))


def test_a_road_reload_recomputes_only_the_network_family(graph, conn):
    _materialize_everything(conn)
    publish_version(conn, asset_key="global.road_network", version="graph-2")

    slugs = set(_slugs(_plan(conn)))

    assert slugs == {
        "accessibility_block_school_network_m",
        "accessibility_block_health_network_m",
        "accessibility_block_park_network_m",
    }


def test_an_explicit_change_set_still_takes_the_attribute_path(graph, conn):
    """Naming attributes keeps working, and keeps its old meaning.

    A caller that genuinely knows something the graph does not must still be
    able to say so — and a scenario what-if is exactly that caller until
    overrides become a versioned asset.
    """
    _materialize_everything(conn)

    slugs = set(_slugs(_plan(conn, changed_attributes=("floors",))))

    assert slugs == {"urban_form_block_planned_floors"}


def test_an_irrelevant_attribute_is_still_a_noop(graph, conn):
    """The conformance suite's contract: an edit no indicator reads does nothing.

    Worth pinning here too, because the freshness branch must not quietly take
    over when a change-set was supplied and simply matched nothing.
    """
    _materialize_everything(conn)
    publish_version(conn, asset_key="global.building", version="release-2")

    assert _plan(conn, changed_attributes=("nothing_reads_this",)).is_noop


def test_a_partial_materialization_counts_as_dirty(graph, conn):
    """A partition written over a subset of its features is not finished."""
    _materialize_everything(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset_materialization "
            "SET coverage_scope = '{\"complete\": false}'::jsonb "
            "WHERE asset_id = (SELECT id FROM curated_geo.asset "
            "WHERE asset_key = 'indicator.urban_form_block_planned_floors')"
        )
    assert "urban_form_block_planned_floors" in _slugs(_plan(conn))


def test_planning_still_works_without_the_asset_graph(conn):
    """A database still on r040 must plan runs exactly as it always did.

    Probed with to_regclass rather than by catching the error: a failed query
    leaves psycopg2's transaction aborted, which would poison the connection the
    caller is about to submit on.
    """
    with conn.cursor() as cur:
        # CASCADE because explore.asset_freshness_v reads it — the same
        # dependency the r041 downgrade drops in order.
        cur.execute("DROP VIEW serving_geo.asset_freshness_v CASCADE")

    assert _plan(conn).is_noop
    assert _slugs(_plan(conn, changed_attributes=("floors",))) == [
        "urban_form_block_planned_floors"
    ]


# ── scenarios, as the same mechanism ───────────────────────────────────────


def _block_id(conn):
    from tests.integration._helpers import insert_feature, register_source, start_run

    source_id = register_source(
        conn, name="scn-src", storage_type="vector", integration_role="canonical_entity"
    )
    run_id = start_run(conn, source_id)
    from reimagina.data.feature import get_type_id

    return insert_feature(
        conn,
        type_id=get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="blk-1",
        geometry_wkt="POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))",
    )


def _edit(conn, scenario_id, feature_id, *, attribute="floors", value=9.0):
    from reimagina.scenarios.overrides import apply_override

    apply_override(
        conn,
        scenario_id=scenario_id,
        feature_id=feature_id,
        attribute_key=attribute,
        value_numeric=value,
    )


SCENARIO = "11111111-1111-1111-1111-111111111111"
OTHER_SCENARIO = "22222222-2222-2222-2222-222222222222"


def test_a_what_if_recomputes_the_attribute_readers(graph, conn):
    """The unification: an edit is a version bump, not a second mechanism.

    ⚠ **Scenario-wide granularity, not per-attribute.** The override version is
    a digest of the whole scenario's rows, so editing `floors` also marks every
    other attribute-reading indicator dirty. That is the same over-approximation
    `resolve_change_set` already made — it returns the flat union of a
    scenario's attributes — so this is not a regression, but neither is it the
    exactness the baseline path now has.

    Making it exact means partitioning the override asset by attribute as well
    as scenario, so an indicator's edge resolves only to the attributes it
    declares. That needs a partition mapping keyed on the edge rather than the
    node, which the view does not have.

    The composite is excluded for a different and correct reason: it reads no
    attributes, so a what-if reaches it through its components rather than
    directly.
    """
    _materialize_everything(conn)
    _edit(conn, SCENARIO, _block_id(conn))

    slugs = set(_slugs(_plan(conn, scenario_id=SCENARIO)))

    assert "urban_form_block_planned_floors" in slugs


def test_a_what_if_leaves_the_baseline_alone(graph, conn):
    """The property that makes scenarios safe to model as versions.

    scenario.override has no baseline partition, so no edit to any scenario can
    move a baseline expected version.
    """
    _materialize_everything(conn)
    _edit(conn, SCENARIO, _block_id(conn))

    assert _plan(conn).is_noop


def test_one_scenario_does_not_disturb_another(graph, conn):
    """Partitioned, so an edit reaches exactly its own slice."""
    _materialize_everything(conn)
    _edit(conn, SCENARIO, _block_id(conn))

    assert _plan(conn, scenario_id=OTHER_SCENARIO).is_noop
    assert not _plan(conn, scenario_id=SCENARIO).is_noop


def test_an_unedited_scenario_computes_nothing(graph, conn):
    """Where a scenario's expected version equals baseline, its answer IS baseline.

    Computing it anyway would write a duplicate row and call it a what-if.
    """
    _materialize_everything(conn)
    assert _plan(conn, scenario_id=SCENARIO).is_noop


def test_reapplying_the_same_edit_publishes_nothing(graph, conn):
    """The override version is a content digest, not an event id.

    Saving a what-if twice is not a change, and must not queue a recompute.
    """
    _materialize_everything(conn)
    block = _block_id(conn)
    _edit(conn, SCENARIO, block)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sequence FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = 'scenario.override' AND av.is_current"
        )
        first = cur.fetchone()[0]

    _edit(conn, SCENARIO, block)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sequence FROM curated_geo.asset_version av "
            "JOIN curated_geo.asset a ON a.id = av.asset_id "
            "WHERE a.asset_key = 'scenario.override' AND av.is_current"
        )
        assert cur.fetchone()[0] == first


def test_a_layer_reload_reaches_scenario_partitions_too(graph, conn):
    """Dataset edges map to baseline, so a scenario reads the one shared layer.

    If they mapped by identity, a scenario partition would look for a
    `scenario=S` copy of the blocks, find nothing, and never notice the layer
    changed at all.
    """
    _materialize_everything(conn)
    _edit(conn, SCENARIO, _block_id(conn))
    # Materialize the scenario partition so it starts fresh.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization
                (asset_id, partition_key, version)
            SELECT e.asset_id, e.partition_key, e.expected_version
            FROM serving_geo.asset_expected_version_v e
            JOIN curated_geo.asset a ON a.id = e.asset_id
            WHERE a.kind = 'derived' AND e.partition_key = %s
            """,
            (f"scenario={SCENARIO}",),
        )
    assert _plan(conn, scenario_id=SCENARIO).is_noop

    publish_version(conn, asset_key="mex.block", version="blocks-2")

    assert not _plan(conn, scenario_id=SCENARIO).is_noop


# ── config is data ──────────────────────────────────────────────────────────


def test_editing_a_catalog_bound_stales_only_that_indicator(graph, conn):
    """`valid_max` lives in a table, so changing it needs no commit.

    Not hypothetical: edificability's bound was 10,000, from an assumption that
    a CDMX manzana tops out near 4 ha. False of Tlalpan, where it rejected 71 of
    5,273 blocks and had to become 200,000. An edit like that has to restage the
    results it changes, and a per-asset `code_version` would never notice.
    """
    _materialize_everything(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset SET params_digest = 'edited' "
            "WHERE asset_key = 'indicator.urban_form_block_planned_floors'"
        )

    slugs = set(_slugs(_plan(conn)))

    assert slugs == {"urban_form_block_planned_floors"}


def test_a_params_edit_propagates_to_dependents(graph, conn):
    """A composite reads its components, so their config reaches it too."""
    _materialize_everything(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset SET params_digest = 'edited' "
            "WHERE asset_key = 'indicator.urban_form_block_compactness'"
        )

    slugs = set(_slugs(_plan(conn)))

    assert slugs == {
        "urban_form_block_compactness",
        "urban_form_block_composite",
    }
