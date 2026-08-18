"""Deriving the dirty set from freshness rather than from declared attributes.

The attribute path answers "an edit named X, who reads X". This one answers "what
is actually out of date", which is a different and larger question: it also
catches a layer that was reloaded, an asset whose own logic version moved, and
anything left half-finished by a crash — none of which any edit names.

⚠ ``absent`` is excluded by default and that is a deliberate safety choice. A
partition nobody has ever computed is not *invalidated*; on a fresh database
every indicator is absent, and treating that as a dirty set would turn an empty
submit into a full recompute of the platform. Absence is a real state worth
acting on, but it should be asked for.

Ordering reuses ``orchestration.graph.topological_order`` rather than sorting
here. That function's determinism is load-bearing — the ops-plane replays a
workflow by matching steps positionally — and a second ordering implementation
would be a second thing to keep deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from reimagina.assets.keys import INDICATOR_PREFIX
from reimagina.orchestration.graph import topological_order

#: States that mean "this needs recomputing". `partial` is included: a partition
#: materialized over a subset of its features is not finished, and reporting it
#: as done is the specific lie the coverage record exists to prevent.
DIRTY_STATES = ("stale", "partial")


def graph_available(conn: Any) -> bool:
    """Whether the asset graph can actually answer questions in this database.

    Two conditions, and the second is the one that bites. A database migrated to
    r041 but never seeded has every view and no rows: freshness would answer
    "nothing is dirty" about every indicator, which reads as a correct answer and
    would silently turn every scenario run into a noop. An empty registry means
    the graph is not in use here, not that everything is up to date.

    A probe rather than a caught exception. Querying a missing relation raises,
    and in psycopg2 that leaves the transaction aborted — poisoning a connection
    the caller is about to submit a run on. ``to_regclass`` answers NULL for a
    missing relation without erroring, so a database still on r040 stays usable.
    The two steps cannot be one statement: Postgres plans the whole query, so a
    reference to a missing table fails even when guarded by an AND.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('serving_geo.asset_freshness_v') IS NOT NULL")
        if not cur.fetchone()[0]:
            return False
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM curated_geo.asset WHERE kind = 'derived')"
        )
        return bool(cur.fetchone()[0])


def stale_indicator_slugs(
    conn: Any,
    *,
    partition_key: str = "",
    include_absent: bool = False,
) -> list[str]:
    """Indicator slugs whose results are out of date, unordered."""
    if not graph_available(conn):
        return []
    states = list(DIRTY_STATES) + (["absent"] if include_absent else [])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key FROM serving_geo.asset_freshness_v "
            "WHERE kind = 'derived' AND partition_key = %s AND state = ANY(%s) "
            "AND asset_key LIKE %s",
            (partition_key, states, f"{INDICATOR_PREFIX}%"),
        )
        return [row[0][len(INDICATOR_PREFIX) :] for row in cur.fetchall()]


def scenario_dirty_slugs(conn: Any, scenario_id: str) -> list[str]:
    """Indicators a what-if reaches, and has not yet computed at that version.

    Membership is decided by one thing: whether the indicator has a node in this
    scenario's partition at all. ``asset_component_v`` propagates a partition
    downstream along ``identity`` edges, so an indicator gets a ``scenario=S``
    row only where that propagation carried it there from
    ``scenario.override@scenario=S``. An asset the override cannot reach has no
    row and is never returned — no predicate needed, and none possible.

    ⚠ The reach is currently the whole catalog, and that bound is worth stating
    plainly. ``scenario.override`` is *one* asset covering every attribute, so
    ``registry._edges_for`` gives every attribute-reading indicator an identity
    edge to it and everything downstream inherits the partition. An override on
    ``floors`` therefore returns all 13 indicators, not the ones that read
    ``floors``: correct, not minimal — the same over-approximation
    ``resolve_change_set`` documents, one layer further down. Splitting the
    override into one asset per attribute is what makes it exact, and this query
    needs no change when that lands, because the unreached indicators simply
    stop having a node.

    ⚠ Do not narrow this by comparing expected versions. An earlier revision
    filtered on ``expected(scenario) <> expected(baseline)`` and that predicate
    is a tautology: every component embeds the partition it was reached at,
    beginning with the root's own (``asset_key || '@' || partition_key || '='``
    in r041), so two partitions of one asset can never hash alike. It excluded
    nothing while reading as though it did the whole job.

    ``absent`` counts here, unlike on the baseline. A scenario partition nobody
    has computed is precisely what a what-if submit is asking for, whereas an
    absent baseline is just a database nobody has run yet.
    """
    if not graph_available(conn):
        return []
    from reimagina.assets.keys import partition_key as make_partition_key

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.asset_key
            FROM serving_geo.asset_expected_version_v e
            LEFT JOIN serving_geo.asset_freshness_v f
              ON f.asset_id = e.asset_id
             AND f.partition_key = e.partition_key
            WHERE e.partition_key = %s
              AND e.asset_key LIKE %s
              AND (f.materialized_version IS NULL
                   OR f.materialized_version <> e.expected_version
                   OR NOT f.coverage_complete)
            """,
            (make_partition_key({"scenario": scenario_id}), f"{INDICATOR_PREFIX}%"),
        )
        return [row[0][len(INDICATOR_PREFIX) :] for row in cur.fetchall()]


def stale_units(
    conn: Any,
    registry: Sequence[Any],
    *,
    partition_key: str = "",
    include_absent: bool = False,
    scenario_id: str | None = None,
) -> list[str]:
    """Out-of-date indicator slugs, in dependency order.

    Only slugs the registry knows are returned: an asset row for an indicator
    that has since been removed from the catalog has no runner, and dispatching
    it would fail a unit rather than skip it.
    """
    known = {spec.slug for spec in registry}
    candidates = (
        scenario_dirty_slugs(conn, scenario_id)
        if scenario_id
        else stale_indicator_slugs(
            conn, partition_key=partition_key, include_absent=include_absent
        )
    )
    dirty = {slug for slug in candidates if slug in known}
    if not dirty:
        return []
    deps = {spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry}
    return list(topological_order(deps, nodes=dirty))
