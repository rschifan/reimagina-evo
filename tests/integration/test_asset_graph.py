"""The asset graph against real Postgres: constraints, seeding, and the algebra.

The parity test here is the load-bearing one. ``serving_geo.asset_expected_version_v``
and ``reimagina.assets.version`` derive the same value by different routes; if
they ever disagree, staleness forks in two, each half internally consistent
while disagreeing about what is out of date. Nothing else in this suite would
notice.
"""

from __future__ import annotations

import pytest

from reimagina.assets.keys import indicator_asset_key
from reimagina.assets.registry import (
    SHIPPED_ASSETS,
    scenario_override_assets,
    seed_assets,
)
from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.assets.version import components, expected_versions


def _sql_components(conn) -> dict[tuple[str, str], set[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, partition_key, part FROM serving_geo.asset_component_v"
        )
        out: dict[tuple[str, str], set[str]] = {}
        for asset_key, partition_key, part in cur.fetchall():
            out.setdefault((asset_key, partition_key), set()).add(part)
        return out


def assert_parity(conn):
    """Compare component sets first, hashes second.

    The component diff is what makes a divergence diagnosable: it names the term
    that drifted, where a hash comparison would only say the two disagree.
    """
    assert components(conn) == _sql_components(conn)
    assert expected_versions(conn) == _sql_versions(conn)


def _add_asset(conn, key, kind, **kwargs):
    """Insert one asset, returning its id."""
    columns = {
        "asset_key": key,
        "kind": kind,
        "backing_kind": kwargs.pop("backing_kind", "indicator"),
        **kwargs,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO curated_geo.asset ({names}) VALUES ({placeholders}) "
            "RETURNING id",
            tuple(columns.values()),
        )
        return cur.fetchone()[0]


def _add_edge(conn, consumer, producer, relation, **kwargs):
    columns = {
        "consumer_asset_id": consumer,
        "producer_asset_id": producer,
        "relation": relation,
        **kwargs,
    }
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO curated_geo.asset_dependency ({names}) "
            f"VALUES ({placeholders})",
            tuple(columns.values()),
        )


def _record_version(conn, asset_id, version, sequence=1):
    """Publish a new current version, retiring the previous one."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset_version SET is_current = false, "
            "valid_to = now() WHERE asset_id = %s AND is_current",
            (asset_id,),
        )
        cur.execute(
            "INSERT INTO curated_geo.asset_version (asset_id, version, sequence) "
            "VALUES (%s, %s, %s)",
            (asset_id, version, sequence),
        )


def _freshness(conn) -> dict[str, str]:
    """Freshness for this module's synthetic assets only.

    Scoped to the `t.` prefix rather than reading the whole view: the registry is
    seeded into any real database, so an assertion over every asset would pass or
    fail depending on what else happens to be registered.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, state FROM serving_geo.asset_freshness_v "
            "WHERE partition_key = '' AND asset_key LIKE 't.%'"
        )
        return dict(cur.fetchall())


def _sql_versions(conn) -> dict[tuple[str, str], str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, partition_key, expected_version "
            "FROM serving_geo.asset_expected_version_v"
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def _materialize_all_derived(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization (asset_id, version)
            SELECT e.asset_id, e.expected_version
            FROM serving_geo.asset_expected_version_v e
            JOIN curated_geo.asset a ON a.id = e.asset_id
            WHERE e.partition_key = '' AND a.kind = 'derived'
              AND a.asset_key LIKE 't.%'
            """
        )


@pytest.fixture
def graph(conn):
    """The shipped urban-form shape: two indicators over shared and private inputs.

    block ──self──────────> compactness ──derived_from(0.5)──> composite
      └────self──────────> density      ──derived_from(0.5)──┘
    building ──child_in_parent──> density

    Chosen because it is a diamond (block is reachable from the composite by two
    paths) and because compactness and density share one input but not the other
    — which is exactly the discrimination the old `reads=("geometry",)` world
    could not express.
    """
    ids = {
        "block": _add_asset(
            conn,
            "t.block",
            "source",
            backing_kind="feature_type",
            version_strategy="commit",
        ),
        "building": _add_asset(
            conn,
            "t.building",
            "source",
            backing_kind="feature_type",
            version_strategy="commit",
        ),
    }
    for key in ("compactness", "density", "composite"):
        ids[key] = _add_asset(
            conn, f"t.{key}", "derived", producer=f"compute_{key}", code_version="1"
        )
    _record_version(conn, ids["block"], "v1")
    _record_version(conn, ids["building"], "v1")

    _add_edge(conn, ids["compactness"], ids["block"], "self")
    _add_edge(conn, ids["density"], ids["block"], "self")
    _add_edge(conn, ids["density"], ids["building"], "child_in_parent")
    _add_edge(conn, ids["composite"], ids["compactness"], "derived_from", weight=0.5)
    _add_edge(conn, ids["composite"], ids["density"], "derived_from", weight=0.5)
    return ids


# ── the algebra ────────────────────────────────────────────────────────────


def test_python_and_sql_agree_on_every_expected_version(conn, graph):
    """The parity gate. See the module docstring for why this one matters most."""
    assert_parity(conn)


def test_parity_survives_a_radius_edge(conn, graph):
    """Pins the float-formatting agreement.

    Postgres renders 1500.0::double precision::text as '1500' and Python's str()
    gives '1500.0'. 1500.0 is ACCESSIBILITY_CUTOFF_M — the only radius the
    catalog ships — so this is the value the two implementations would actually
    have disagreed on.
    """
    poi = _add_asset(
        conn, "t.poi", "source", backing_kind="feature_type", version_strategy="commit"
    )
    _record_version(conn, poi, "v1")
    _add_edge(conn, graph["compactness"], poi, "within_radius", radius_m=1500.0)
    assert_parity(conn)


def test_a_source_bump_moves_only_its_descendants(conn, graph):
    """The whole design in one assertion.

    Bumping the building layer must move density and the composite above it, and
    must leave compactness alone — it never reads buildings.
    """
    before = _sql_versions(conn)
    _record_version(conn, graph["building"], "v2", sequence=2)
    after = _sql_versions(conn)

    assert after[("t.compactness", "")] == before[("t.compactness", "")]
    assert after[("t.block", "")] == before[("t.block", "")]
    assert after[("t.density", "")] != before[("t.density", "")]
    assert after[("t.composite", "")] != before[("t.composite", "")]


def test_freshness_reports_only_the_affected_indicators_stale(conn, graph):
    _materialize_all_derived(conn)
    assert _freshness(conn) == {
        "t.block": "fresh",
        "t.building": "fresh",
        "t.compactness": "fresh",
        "t.density": "fresh",
        "t.composite": "fresh",
    }

    _record_version(conn, graph["building"], "v2", sequence=2)
    after = _freshness(conn)
    assert after["t.compactness"] == "fresh"
    assert after["t.density"] == "stale"
    assert after["t.composite"] == "stale"


def test_a_code_version_bump_moves_only_that_asset_and_below(conn, graph):
    """Per-asset code versions, not the git SHA.

    A repo-wide version in the fingerprint would make every commit invalidate
    every derived asset; this asserts the opposite.
    """
    before = _sql_versions(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset SET code_version = '2' WHERE asset_key = %s",
            ("t.density",),
        )
    after = _sql_versions(conn)

    assert after[("t.density", "")] != before[("t.density", "")]
    assert after[("t.composite", "")] != before[("t.composite", "")]
    assert after[("t.compactness", "")] == before[("t.compactness", "")]


def test_reshaping_an_edge_moves_the_version(conn, graph):
    """Closure membership is unchanged here — only the projection narrows.

    Dagster names "dependencies added or removed" as a stale cause; an edge whose
    *shape* changes is the same class of change and has to count.
    """
    before = _sql_versions(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset_dependency SET attributes = ARRAY['floors'] "
            "WHERE consumer_asset_id = %s AND relation = 'self'",
            (graph["compactness"],),
        )
    assert _sql_versions(conn)[("t.compactness", "")] != before[("t.compactness", "")]


def test_never_materialized_derived_asset_is_absent_not_stale(conn, graph):
    assert _freshness(conn)["t.density"] == "absent"


def test_a_source_asset_is_never_stale(conn, graph):
    """A source has nothing to be stale against — its recorded version is truth.

    Comparing it to the closure hash would mark every ingested layer permanently
    stale and make `assets stale` unreadable.
    """
    _record_version(conn, graph["block"], "v9", sequence=2)
    assert _freshness(conn)["t.block"] == "fresh"


def test_partial_coverage_reports_partial_not_fresh(conn, graph):
    """A partition is not atomically materialized.

    bulk_write_numeric_results stales only the feature ids it wrote, and
    indicators/edificability.py relies on that to skip features missing an
    attribute — so a partition legitimately holds rows from several runs.
    """
    _materialize_all_derived(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE curated_geo.asset_materialization "
            "SET coverage_scope = '{\"complete\": false}'::jsonb "
            "WHERE asset_id = %s",
            (graph["density"],),
        )
    assert _freshness(conn)["t.density"] == "partial"


def test_lineage_answers_both_questions(conn, graph):
    """Upstream: what does this composite need. Downstream: what must recompute."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT related_key FROM serving_geo.asset_lineage_v "
            "WHERE direction = 'upstream' AND root_key = 't.composite'"
        )
        upstream = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT related_key FROM serving_geo.asset_lineage_v "
            "WHERE direction = 'downstream' AND root_key = 't.building'"
        )
        downstream = {r[0] for r in cur.fetchall()}

    assert upstream == {"t.compactness", "t.density", "t.block", "t.building"}
    assert downstream == {"t.density", "t.composite"}


# ── constraints ────────────────────────────────────────────────────────────


def _expect_violation(conn, fn, match: str):
    """Run `fn`, forcing deferred constraints to fire, and expect a failure.

    The validate trigger is DEFERRABLE INITIALLY DEFERRED so that seeding can
    write a composite's edges one row at a time; the `conn` fixture never
    commits, so without SET CONSTRAINTS the trigger would never run at all and
    these tests would pass vacuously.
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_violation")
    with pytest.raises(Exception, match=match):
        fn()
        with conn.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT before_violation")


def test_trigger_rejects_a_cycle(conn, graph):
    _expect_violation(
        conn,
        lambda: _add_edge(conn, graph["block"], graph["composite"], "self"),
        "cycle|must reference a derived asset",
    )


def test_trigger_rejects_weights_that_do_not_sum_to_one(conn):
    parent = _add_asset(conn, "t.parent", "derived", producer="p", code_version="1")
    child = _add_asset(conn, "t.child", "derived", producer="c", code_version="1")
    _expect_violation(
        conn,
        lambda: _add_edge(conn, parent, child, "derived_from", weight=0.5),
        "must sum to 1.0",
    )


def test_trigger_rejects_a_non_derived_consumer(conn, graph):
    """Only a derived asset can consume: a source is produced outside the graph."""
    _expect_violation(
        conn,
        lambda: _add_edge(conn, graph["block"], graph["building"], "self"),
        "must reference a derived asset",
    )


def test_radius_is_required_exactly_for_within_radius(conn, graph):
    with pytest.raises(Exception, match="asset_dependency_radius"):
        _add_edge(conn, graph["compactness"], graph["building"], "within_radius")


# ── seeding ────────────────────────────────────────────────────────────────


def test_seed_assets_is_idempotent_and_stable(conn):
    """Re-seeding must keep ids: an asset's version history hangs off them."""
    first = seed_assets(conn)
    second = seed_assets(conn)
    seeded = [r["asset_key"] for r in first]
    # Datasets first, then one derived asset per shipped indicator — projected
    # from SHIPPED_INDICATORS rather than listed again, so the two cannot
    # disagree about which indicators exist.
    # Datasets, then one override asset per overridable attribute, then one
    # derived asset per shipped indicator — the last two projected from
    # SHIPPED_INDICATORS rather than listed again, so none of the three can
    # disagree about which indicators exist or what they read.
    assert seeded == [
        *(s.asset_key for s in SHIPPED_ASSETS),
        *(s.asset_key for s in scenario_override_assets()),
        *(indicator_asset_key(s.slug) for s in SHIPPED_INDICATORS),
    ]
    assert first == second


def test_seeded_feature_layers_resolve_their_feature_type(conn):
    seed_assets(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.asset "
            "WHERE backing_kind = 'feature_type' AND feature_type_id IS NULL"
        )
        assert cur.fetchone()[0] == 0


def test_road_network_is_external_and_observed(conn):
    """It has no ingestion_run, so nothing here can publish a version for it."""
    seed_assets(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, version_strategy, probe_sql IS NOT NULL "
            "FROM curated_geo.asset WHERE asset_key = 'global.road_network'"
        )
        assert cur.fetchone() == ("external", "observed", True)


# ── the shipped graph ───────────────────────────────────────────────────────


@pytest.fixture
def seeded_graph(conn):
    """The real registry, seeded, with deferred validation forced to run.

    SET CONSTRAINTS is what makes this a guard rather than a formality: the
    acyclicity and weight-sum checks are DEFERRABLE INITIALLY DEFERRED and the
    `conn` fixture never commits, so without it a broken shipped declaration
    would sail through.
    """
    seed_assets(conn)
    with conn.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return conn


def _lineage(conn, direction, root):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT related_key FROM serving_geo.asset_lineage_v "
            "WHERE direction = %s AND root_key = %s",
            (direction, root),
        )
        return {r[0] for r in cur.fetchall()}


def test_the_shipped_declarations_form_a_valid_graph(seeded_graph):
    """Seeding the real registry must satisfy every constraint in r041."""
    with seeded_graph.cursor() as cur:
        cur.execute("SELECT count(*) FROM curated_geo.asset_dependency")
        assert cur.fetchone()[0] > 0


def test_the_composite_reports_the_datasets_it_needs(seeded_graph):
    """Requirement A: a composite's dataset set, deduped, never declared.

    urban_form_block_composite declares only its two components; that it needs
    blocks *and* buildings is derived from what those components consume.
    """
    upstream = _lineage(
        seeded_graph, "upstream", "indicator.urban_form_block_composite"
    )
    assert {"mex.block", "global.building"} <= upstream
    assert {
        "indicator.urban_form_block_compactness",
        "indicator.urban_form_block_building_density",
    } <= upstream


def test_changing_the_road_network_names_exactly_the_network_indicators(seeded_graph):
    """Requirement B, and the discrimination the old world could not express.

    `reads=("geometry",)` could not distinguish "the roads changed" from "the
    blocks changed". Here the three straight-line accessibility indicators are
    untouched by a road edit because they consume POIs, not the network.

    `livability_block_score` is in the set because it is a weighted consumer of
    all three network indicators — a road edit reaches it transitively, which is
    the propagation this graph exists to compute. Its straight-line siblings
    still stay out.
    """
    downstream = _lineage(seeded_graph, "downstream", "global.road_network")
    assert downstream == {
        "indicator.accessibility_block_school_network_m",
        "indicator.accessibility_block_health_network_m",
        "indicator.accessibility_block_park_network_m",
        "indicator.livability_block_score",
    }
    # The discrimination that matters: a road edit must not reach the
    # edificability or urban-form families, which read attributes and geometry
    # rather than the network.
    assert not any(
        node.startswith(("indicator.edificability_", "indicator.urban_form_"))
        for node in downstream
    )


def test_changing_buildings_spares_compactness(seeded_graph):
    """The control case: compactness reads blocks only."""
    downstream = _lineage(seeded_graph, "downstream", "global.building")
    assert "indicator.urban_form_block_building_density" in downstream
    assert "indicator.urban_form_block_composite" in downstream
    assert "indicator.urban_form_block_compactness" not in downstream


def test_reseeding_removes_an_input_that_was_withdrawn(conn):
    """A declaration is replaced, not merged.

    A phantom edge left behind by a removed input would keep invalidating an
    indicator forever, with nothing in the declaration to explain why.
    """
    seed_assets(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM curated_geo.asset WHERE asset_key = 'mex.lot'")
        lot = cur.fetchone()[0]
        cur.execute(
            "SELECT id FROM curated_geo.asset "
            "WHERE asset_key = 'indicator.urban_form_block_compactness'"
        )
        compactness = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO curated_geo.asset_dependency "
            "(consumer_asset_id, producer_asset_id, relation) VALUES (%s, %s, 'self')",
            (compactness, lot),
        )
    seed_assets(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.asset_dependency "
            "WHERE consumer_asset_id = %s AND producer_asset_id = %s",
            (compactness, lot),
        )
        assert cur.fetchone()[0] == 0


# ── scenario partitions ─────────────────────────────────────────────────────


SCENARIO = "77777777-7777-7777-7777-777777777777"


def _dirty(conn, scenario_id=SCENARIO):
    from reimagina.assets.freshness import scenario_dirty_slugs

    return set(scenario_dirty_slugs(conn, scenario_id))


def _publish_override(
    conn, attribute="floors", version="digest-1", scenario_id=SCENARIO
):
    from reimagina.assets.keys import partition_key, scenario_override_asset_key
    from reimagina.assets.publish import publish_version

    publish_version(
        conn,
        asset_key=scenario_override_asset_key(attribute),
        version=version,
        partition_key=partition_key({"scenario": scenario_id}),
    )


#: What each attribute reaches, written out rather than derived from the specs.
#: A helper that re-walked the catalog could agree with a broken graph; these
#: cannot — and did not: `livability_block_score` landed on main while this was
#: in review, and these sets are what noticed that `num_levels` now reaches it
#: through `edificability_block_density`.
#:
#: `floors` is the narrow case — one reader, nothing above it. The other two
#: each propagate past their readers into a composite.
FLOORS_READERS = {"urban_form_block_planned_floors"}
GEOMETRY_READERS = {
    "urban_form_block_compactness",
    "urban_form_block_building_density",
    "urban_form_block_composite",
}
NUM_LEVELS_READERS = {
    "edificability_block_housing_units",
    "edificability_block_population",
    "edificability_block_density",
    # Combines edificability_block_density with the network accessibilities.
    "livability_block_score",
}


def test_a_scenario_nobody_has_edited_is_not_dirty(seeded_graph):
    """No override version means no scenario partition, so nothing to compute."""
    assert _dirty(seeded_graph) == set()


def test_an_override_reaches_exactly_the_indicators_that_read_that_attribute(
    seeded_graph,
):
    """The exactness the per-attribute split buys.

    One override asset per attribute means editing `floors` publishes a version
    for `scenario.override.floors` and nothing else, so the scenario partition
    propagates to the one indicator declaring an edge to `floors` and stops.
    With a single override asset this returned all thirteen.
    """
    _publish_override(seeded_graph, "floors")

    assert _dirty(seeded_graph) == FLOORS_READERS


def test_the_partition_propagates_past_a_reader_to_what_depends_on_it(seeded_graph):
    """Exact does not mean shallow: downstream still follows.

    A composite reads no attribute at all, so it can only be reached through its
    components — which is what `geometry` does and `floors` does not.
    """
    _publish_override(seeded_graph, "geometry")

    assert _dirty(seeded_graph) == GEOMETRY_READERS


def test_two_attributes_reach_two_different_sets(seeded_graph):
    """Different assets, so the sets are genuinely independent — not one bit."""
    _publish_override(seeded_graph, "num_levels")

    assert _dirty(seeded_graph) == NUM_LEVELS_READERS

    _publish_override(seeded_graph, "floors")

    assert _dirty(seeded_graph) == NUM_LEVELS_READERS | FLOORS_READERS


def test_an_override_of_an_attribute_nobody_reads_computes_nothing(seeded_graph):
    """No edge, no propagation. The asset is not even registered."""
    from reimagina.assets.keys import partition_key
    from reimagina.assets.publish import publish_version

    published = publish_version(
        seeded_graph,
        asset_key="scenario.override.not_an_input",
        version="d",
        partition_key=partition_key({"scenario": SCENARIO}),
    )

    assert published is None, "an unregistered asset is not an error, just inert"
    assert _dirty(seeded_graph) == set()


def test_comparing_expected_versions_cannot_narrow_a_scenario(seeded_graph):
    """Why `scenario_dirty_slugs` carries no version-comparison predicate.

    An earlier revision filtered on `expected(scenario) <> expected(baseline)`,
    which reads like exactness and is in fact a tautology: every component
    embeds the partition it was reached at, starting with the root's own, so two
    partitions of one asset never hash alike. The predicate excluded nothing.
    Per-attribute assets are what made the filter real, by removing the *node*;
    this pins that the hash comparison still cannot do it.
    """
    _publish_override(seeded_graph, "floors")
    with seeded_graph.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE s.expected_version = b.expected_version),
                   count(*)
            FROM serving_geo.asset_expected_version_v s
            JOIN serving_geo.asset_expected_version_v b
              ON b.asset_id = s.asset_id AND b.partition_key = ''
            WHERE s.partition_key = %s AND s.asset_key LIKE 'indicator.%%'
            """,
            (f"scenario={SCENARIO}",),
        )
        equal, total = cur.fetchone()

    assert total == len(FLOORS_READERS), "only the reached ones have a node"
    assert equal == 0, "and among those, the predicate excludes nothing"


def test_an_indicator_already_computed_for_the_scenario_drops_out(seeded_graph):
    _publish_override(seeded_graph, "floors")
    slug = "urban_form_block_planned_floors"
    with seeded_graph.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.asset_materialization
                   (asset_id, partition_key, version)
            SELECT e.asset_id, e.partition_key, e.expected_version
            FROM serving_geo.asset_expected_version_v e
            WHERE e.asset_key = %s AND e.partition_key = %s
            """,
            (indicator_asset_key(slug), f"scenario={SCENARIO}"),
        )

    assert slug not in _dirty(seeded_graph)


def test_one_scenario_does_not_dirty_another_or_the_baseline(seeded_graph):
    from reimagina.assets.freshness import stale_indicator_slugs

    _publish_override(seeded_graph, "floors")

    assert _dirty(seeded_graph, "88888888-8888-8888-8888-888888888888") == set()
    assert stale_indicator_slugs(seeded_graph) == []
