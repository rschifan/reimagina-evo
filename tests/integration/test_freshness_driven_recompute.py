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
