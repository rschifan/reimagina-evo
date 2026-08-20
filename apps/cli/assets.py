"""CLI helpers for the asset graph.

Reads go through ``explore`` rather than ``serving_geo`` — AGENTS.md keeps CLI
inspection on the engineer-facing surface, and "which indicators must I
recompute" is an inspection question before it is an orchestration one.
"""

from __future__ import annotations

from typing import Any

from reimagina.assets.observe import observe_assets
from reimagina.assets.registry import seed_assets
from reimagina.db import connect


def seed(*, dsn: str) -> dict[str, Any]:
    """Register every shipped asset and its declared edges. Idempotent."""
    with connect(dsn) as conn:
        rows = seed_assets(conn)
        conn.commit()
    return {"dsn": dsn, "seeded_count": len(rows)}


def observe(*, dsn: str, asset_key: str | None = None) -> dict[str, Any]:
    """Probe observable assets and publish any that changed.

    This is what closes the loop for data no pipeline produces — the road graph
    loaded straight from Overpass, the hand-copied mirrors, anything written by
    raw SQL. Without it those assets report themselves fresh forever.
    """
    with connect(dsn) as conn:
        results = observe_assets(conn, asset_keys=[asset_key] if asset_key else None)
        conn.commit()
    return {
        "observed": len(results),
        "changed": [o.asset_key for o in results if o.changed],
        "failed": {o.asset_key: o.error for o in results if o.error},
        "assets": [
            {
                "asset_key": o.asset_key,
                "version": o.version,
                "changed": o.changed,
                "error": o.error,
            }
            for o in results
        ],
    }


def stale(*, dsn: str, include_absent: bool = False) -> dict[str, Any]:
    """What is out of date right now.

    Derived on read by comparing each asset's materialized version against the
    version its inputs imply — so this answers correctly after a crash, a missed
    hook, or a hand-run migration, none of which a marker-based design survives.
    """
    states = ("stale", "partial", "absent") if include_absent else ("stale", "partial")
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, kind, partition_key, state, materialized_at "
            "FROM explore.asset_freshness_v WHERE state = ANY(%s) "
            "ORDER BY state, asset_key, partition_key",
            (list(states),),
        )
        rows = cur.fetchall()
    return {
        "count": len(rows),
        "assets": [
            {
                "asset_key": r[0],
                "kind": r[1],
                "partition": r[2] or "(baseline)",
                "state": r[3],
                "materialized_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ],
    }


def _lineage(dsn: str, asset_key: str, direction: str) -> dict[str, Any]:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT related_key, related_kind, depth FROM explore.asset_lineage_v "
            "WHERE direction = %s AND root_key = %s ORDER BY depth, related_key",
            (direction, asset_key),
        )
        rows = cur.fetchall()
    return {
        "asset_key": asset_key,
        "direction": direction,
        "count": len(rows),
        "related": [{"asset_key": r[0], "kind": r[1], "depth": r[2]} for r in rows],
    }


def inputs(*, dsn: str, asset_key: str) -> dict[str, Any]:
    """Every asset this one consumes, transitively.

    For a composite this is the answer it could not give before: its dataset set
    is derived from what its components consume, never declared on it.
    """
    return _lineage(dsn, asset_key, "upstream")


def consumers(*, dsn: str, asset_key: str) -> dict[str, Any]:
    """Every asset that would need recomputing if this one changed."""
    return _lineage(dsn, asset_key, "downstream")


def backfill_versions(*, dsn: str | None = None) -> dict[str, Any]:
    """Publish versions for source layers loaded before the asset graph existed."""
    from reimagina.assets.publish import backfill_versions as _backfill

    with connect(dsn) as conn:
        published = _backfill(conn)
        conn.commit()
    return {"published": published, "count": len(published)}
