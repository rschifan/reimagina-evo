"""Observing assets this platform does not produce.

Some data arrives without passing through any pipeline. ``curated_geo.road_edge``
is loaded straight from Overpass with no ``ingestion_run``; the Azure mirrors are
copied by hand; ``scripts/seed_demo_attributes.py`` merges directly into
``feature.attributes``. Nothing in the ingest path can publish a version for
those, so nothing would ever mark their consumers stale — and a staleness system
that reports "fresh" for data it cannot see is worse than none, because it is
believed.

An observable asset declares ``probe_sql`` returning a single scalar that changes
when its content changes. Running the probe and publishing the result closes the
loop: this is dbt's source freshness and Dagster's observable source asset, in
the smallest form that works here.

⚠ The probe is a *content signal*, not a content hash. It has to be cheap enough
to run on a schedule, so it trades a vanishingly small chance of collision (a
change that leaves both the row count and the max id identical) for not scanning
the table. Where that trade is unacceptable, the asset should be produced through
a pipeline and use the ``commit`` strategy instead of being observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reimagina.assets.publish import publish_version


@dataclass(frozen=True)
class Observation:
    """The outcome of probing one asset."""

    asset_key: str
    version: str | None
    changed: bool
    error: str | None = None


def observe_assets(
    conn: Any, *, asset_keys: list[str] | None = None
) -> list[Observation]:
    """Run each observable asset's probe and publish any change.

    A probe that raises is reported rather than raised: one misconfigured asset
    must not stop the rest from being observed, and the caller needs to see which
    one failed. A failed probe publishes nothing, so its consumers keep whatever
    state they already had rather than being told a fresh lie.
    """
    with conn.cursor() as cur:
        if asset_keys:
            cur.execute(
                "SELECT asset_key, probe_sql FROM curated_geo.asset "
                "WHERE version_strategy = 'observed' AND asset_key = ANY(%s) "
                "ORDER BY asset_key",
                (asset_keys,),
            )
        else:
            cur.execute(
                "SELECT asset_key, probe_sql FROM curated_geo.asset "
                "WHERE version_strategy = 'observed' ORDER BY asset_key"
            )
        probes = cur.fetchall()

    results: list[Observation] = []
    for asset_key, probe_sql in probes:
        try:
            # A SAVEPOINT, not a bare rollback. A failing probe aborts the
            # transaction and something has to clear that, but `conn.rollback()`
            # would discard everything the caller had done — including versions
            # published for assets probed earlier in this same sweep, which would
            # still be reported as `changed`. Losing a publish while announcing
            # it is the failure this whole design exists to remove.
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT probe")
                try:
                    cur.execute(probe_sql)
                    row = cur.fetchone()
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT probe")
                    raise
                cur.execute("RELEASE SAVEPOINT probe")
            version = "empty" if row is None or row[0] is None else str(row[0])
        except Exception as exc:  # noqa: BLE001 - reported per asset, see docstring
            results.append(
                Observation(
                    asset_key=asset_key, version=None, changed=False, error=str(exc)
                )
            )
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT av.version FROM curated_geo.asset_version av "
                "JOIN curated_geo.asset a ON a.id = av.asset_id "
                "WHERE a.asset_key = %s AND av.is_current AND av.partition_key = ''",
                (asset_key,),
            )
            current = cur.fetchone()
        changed = current is None or current[0] != version
        if changed:
            publish_version(conn, asset_key=asset_key, version=version)
        results.append(
            Observation(asset_key=asset_key, version=version, changed=changed)
        )
    return results
