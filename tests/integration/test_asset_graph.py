"""The asset graph against real Postgres: constraints, seeding, and the algebra.

The parity test here is the load-bearing one. ``serving_geo.asset_expected_version_v``
and ``reimagina.assets.version`` derive the same value by different routes; if
they ever disagree, staleness forks in two, each half internally consistent
while disagreeing about what is out of date. Nothing else in this suite would
notice.
"""

from __future__ import annotations

import pytest

from reimagina.assets.registry import SHIPPED_ASSETS, seed_assets
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
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, state FROM serving_geo.asset_freshness_v "
            "WHERE partition_key = ''"
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
    assert [r["asset_key"] for r in first] == [s.asset_key for s in SHIPPED_ASSETS]
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
