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
    """Whether the asset graph exists in this database.

    A probe rather than a caught exception. Querying a missing relation raises,
    and in psycopg2 that leaves the transaction aborted — poisoning a connection
    the caller is about to submit a run on. ``to_regclass`` answers NULL for a
    missing relation without erroring, so a database still on r040 stays usable.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('serving_geo.asset_freshness_v') IS NOT NULL")
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


def stale_units(
    conn: Any,
    registry: Sequence[Any],
    *,
    partition_key: str = "",
    include_absent: bool = False,
) -> list[str]:
    """Out-of-date indicator slugs, in dependency order.

    Only slugs the registry knows are returned: an asset row for an indicator
    that has since been removed from the catalog has no runner, and dispatching
    it would fail a unit rather than skip it.
    """
    known = {spec.slug for spec in registry}
    dirty = {
        slug
        for slug in stale_indicator_slugs(
            conn, partition_key=partition_key, include_absent=include_absent
        )
        if slug in known
    }
    if not dirty:
        return []
    deps = {spec.slug: {dep for dep, _weight in spec.depends_on} for spec in registry}
    return list(topological_order(deps, nodes=dirty))
