"""Expected versions, computed in Python — the twin of
``serving_geo.asset_expected_version_v``.

Two implementations of one algebra is a deliberate, uncomfortable choice. The
SQL view is what the platform reads: it is always live, costs O(graph) rather
than O(rows), and needs no job to keep it true. This module exists so that
property can be *tested* — a pure-Python derivation of the same value, from the
same rows, by a different route. ``tests/integration/test_asset_graph.py``
asserts they agree for every asset and partition.

If they ever disagree, staleness has silently forked in two: each half stays
internally consistent while disagreeing about what is out of date. That is the
one failure mode this design cannot absorb, which is why the duplication is
worth its cost and why every formatting decision below is pinned to match the
view exactly.

The pinned decisions, each mirroring a comment in r041:

* **Closure hashing, not nested hashing.** A derived asset's version is the hash
  of its whole upstream closure, not a hash of its parents' hashes. Equivalent
  for staleness, and far easier to keep two implementations agreeing on.
* **Components are a set.** Diamonds reach the same node by several paths; a
  multiset would make the hash depend on graph shape rather than content.
* **Floats format at a fixed scale of 6.** Postgres renders
  ``1500.0::double precision::text`` as ``'1500'`` while Python's ``str`` gives
  ``'1500.0'`` — and 1500.0 is ``ACCESSIBILITY_CUTOFF_M``, the only radius the
  catalog ships.
"""

from __future__ import annotations

import hashlib
from typing import Any

from reimagina.assets.keys import BASELINE
from reimagina.storage import backend_for_connection

#: Matches the ``depth < 50`` guard in the recursive view and in
#: ``indicator_dependency_validate`` before it. The acyclicity trigger should
#: make it unreachable; it is a backstop, not a policy.
MAX_DEPTH = 50

_SCALE = 6


def _require_postgres(conn: Any) -> None:
    if backend_for_connection(conn).name != "postgres":
        raise RuntimeError("the asset graph requires the Postgres backend")


def _num(value: float | None) -> str:
    """Format a float the way ``round(x::numeric, 6)::text`` does."""
    return "" if value is None else f"{value:.{_SCALE}f}"


def _node_part(asset: dict[str, Any], partition: str, recorded: str | None) -> str:
    if asset["kind"] == "derived":
        body = f"code:{asset['code_version']}:params:{asset['params_digest'] or ''}"
    else:
        # 'missing' rather than skipping the component: a source that has never
        # been observed must leave everything downstream deterministically stale
        # until it is, not quietly drop out of the hash.
        body = recorded if recorded is not None else "missing"
    return f"{asset['asset_key']}@{partition}={body}"


def _edge_part(edge: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    return (
        f"edge:{by_id[edge['consumer_asset_id']]['asset_key']}"
        f"->{by_id[edge['producer_asset_id']]['asset_key']}"
        f":{edge['relation']}"
        f":{','.join(edge['attributes'])}"
        f":{_num(edge['radius_m'])}"
        f":{_num(edge['weight'])}"
        f":{edge['partition_mapping']}"
    )


def expected_versions(conn: Any) -> dict[tuple[str, str], str]:
    """Return ``{(asset_key, partition_key): expected_version}`` for every node."""
    return {
        node: hashlib.md5(  # noqa: S324 - identity, not a security primitive
            "|".join(sorted(parts)).encode(), usedforsecurity=False
        ).hexdigest()
        for node, parts in components(conn).items()
    }


def components(conn: Any) -> dict[tuple[str, str], set[str]]:
    """Return the component set hashed for each ``(asset_key, partition_key)``.

    Exposed rather than inlined so a parity failure can be *diagnosed*: comparing
    hashes only ever says "these differ", while comparing component sets says
    which term diverged and therefore which formatting rule drifted.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, asset_key, kind, code_version, params_digest "
            "FROM curated_geo.asset"
        )
        assets = [
            {
                "id": r[0],
                "asset_key": r[1],
                "kind": r[2],
                "code_version": r[3],
                "params_digest": r[4],
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT asset_id::text, partition_key, version "
            "FROM curated_geo.asset_version WHERE is_current"
        )
        recorded = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT asset_id::text, partition_key "
            "FROM curated_geo.asset_materialization"
        )
        materialized = {(r[0], r[1]) for r in cur.fetchall()}
        cur.execute(
            "SELECT consumer_asset_id::text, producer_asset_id::text, relation, "
            "attributes, radius_m, weight, partition_mapping "
            "FROM curated_geo.asset_dependency"
        )
        edges = [
            {
                "consumer_asset_id": r[0],
                "producer_asset_id": r[1],
                "relation": r[2],
                "attributes": list(r[3]),
                "radius_m": r[4],
                "weight": r[5],
                "partition_mapping": r[6],
            }
            for r in cur.fetchall()
        ]

    by_id = {a["id"]: a for a in assets}
    out_edges: dict[str, list[dict[str, Any]]] = {a["id"]: [] for a in assets}
    for edge in edges:
        out_edges[edge["consumer_asset_id"]].append(edge)

    # The node universe, mirroring the `node` CTE: every partition with a
    # recorded version, every partition ever materialized, and every asset's
    # baseline partition so a never-computed asset still has something to compare.
    nodes: set[tuple[str, str]] = set(recorded) | materialized
    nodes |= {(a["id"], BASELINE) for a in assets}

    # ...plus propagation downstream along identity edges, so a scenario
    # partition exists for every indicator that scenario reaches, not only for
    # those something has already materialized. Without it the first evaluation
    # of a what-if has nothing to compare and reports "nothing to do".
    consumers_of: dict[str, list[str]] = {a["id"]: [] for a in assets}
    for edge in edges:
        if edge["partition_mapping"] == "identity":
            consumers_of[edge["producer_asset_id"]].append(edge["consumer_asset_id"])
    frontier = [n for n in nodes if n[1] != BASELINE]
    while frontier:
        asset_id, partition = frontier.pop()
        for consumer in consumers_of.get(asset_id, ()):
            if (consumer, partition) not in nodes:
                nodes.add((consumer, partition))
                frontier.append((consumer, partition))

    # Which producer partitions a given consumer partition reaches, per mapping.
    def resolve(edge: dict[str, Any], partition: str) -> list[str]:
        mapping = edge["partition_mapping"]
        if mapping == "baseline":
            return [BASELINE]
        if mapping == "fan_in":
            producer = edge["producer_asset_id"]
            return sorted({p for (a, p) in nodes if a == producer})
        return [partition]

    out: dict[tuple[str, str], set[str]] = {}
    for root in nodes:
        parts: set[str] = set()
        seen: set[tuple[str, str]] = set()
        # Named `stack`, not `frontier`: this walks *upstream* carrying a depth,
        # so its element type is ((asset, partition), depth) rather than the
        # (asset, partition) the propagation frontier above holds. Reusing one
        # name for both shapes is what mypy rejected — the two loops were always
        # correct, they just could not share an inferred type.
        stack: list[tuple[tuple[str, str], int]] = [(root, 0)]
        while stack:
            (asset_id, partition), depth = stack.pop()
            if (asset_id, partition) in seen or depth > MAX_DEPTH:
                continue
            seen.add((asset_id, partition))
            asset = by_id[asset_id]
            parts.add(_node_part(asset, partition, recorded.get((asset_id, partition))))
            for edge in out_edges[asset_id]:
                parts.add(_edge_part(edge, by_id))
                for producer_partition in resolve(edge, partition):
                    stack.append(
                        ((edge["producer_asset_id"], producer_partition), depth + 1)
                    )
        out[(by_id[root[0]]["asset_key"], root[1])] = parts
    return out
