"""Scenario overrides + effective attribute resolution — the what-if mechanism.

A scenario's attribute edits live in ``curated_geo.scenario_override``. The
effective value of an attribute for a feature under a scenario is
``override ⊕ current observation ⊕ base feature.attributes`` (first non-null
wins). Baseline (``scenario_id=None``) collapses to ``observation ⊕ base``.

Postgres-only — matches the async/orchestration Postgres path.
The impact engine needs no change: an override that edits ``attribute_key`` is
exactly the ``changed_attributes`` token it already keys the dirty-set off.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from reimagina.storage import backend_for_connection


def _require_postgres(conn: Any) -> None:
    if backend_for_connection(conn).name != "postgres":
        raise RuntimeError("scenario overrides require the Postgres backend")


def apply_override(
    conn: Any,
    *,
    scenario_id: str,
    feature_id: str,
    attribute_key: str,
    value_numeric: float | None = None,
    value_text: str | None = None,
    publish: bool = True,
) -> None:
    """Upsert one scenario attribute edit (the what-if).

    ``publish`` writes a new version for this scenario's partition of
    ``scenario.override.<attribute_key>``, which is what makes the edit visible
    to freshness. Pass ``publish=False`` only when batching —
    ``apply_overrides`` does, and publishes once per attribute at the end,
    because the version digests every override row for that attribute in the
    scenario and doing that per edit is quadratic.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.scenario_override
              (scenario_id, feature_id, attribute_key, value_numeric, value_text)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (scenario_id, feature_id, attribute_key)
            DO UPDATE SET value_numeric = EXCLUDED.value_numeric,
                          value_text = EXCLUDED.value_text
            """,
            (scenario_id, feature_id, attribute_key, value_numeric, value_text),
        )
    if publish:
        publish_scenario_version(conn, scenario_id, [attribute_key])


def apply_overrides(conn: Any, *, scenario_id: str, edits: Iterable[dict]) -> int:
    """Apply many edits to one scenario, publishing each touched attribute once.

    ⚠ Use this for anything that edits more than one feature. An attribute's
    version is a digest over *every* override row it has in the scenario, so
    publishing per edit is quadratic: upzoning 10,000 blocks one call at a time
    digests 1 row, then 2, then 10,000 — roughly 50 million row reads to record
    10,000 upserts.

    Each edit is a dict of the keyword arguments ``apply_override`` takes, minus
    ``scenario_id``. Returns the number applied.
    """
    count = 0
    touched: set[str] = set()
    for edit in edits:
        apply_override(conn, scenario_id=scenario_id, publish=False, **edit)
        touched.add(edit["attribute_key"])
        count += 1
    if touched:
        publish_scenario_version(conn, scenario_id, touched)
    return count


def publish_scenario_version(
    conn: Any, scenario_id: str, attributes: Iterable[str] | None = None
) -> dict[str, str | None]:
    """Record this scenario's edits as versions of the per-attribute override assets.

    Returns ``{attribute: version}``, with None where the attribute's version was
    *retired* because the scenario no longer edits it.

    One version per attribute, not one for the scenario, and that is what makes
    a what-if exact. ``scenario.override.floors@scenario=S`` and
    ``scenario.override.num_levels@scenario=S`` are partitions of different
    assets, so editing ``floors`` bumps the first alone and only the indicators
    that declared an edge to ``floors`` go out of date. A single scenario-wide
    version made every attribute-reading indicator a consumer of every edit.

    A content digest per attribute rather than an event id, and cheap because it
    is: a scenario's edits to one attribute are a handful of rows, not a layer.
    Digesting means re-applying an edit that changes nothing publishes nothing,
    so a what-if is not recomputed for being saved twice.

    ``attributes`` narrows the work to the ones a caller just touched. Left
    unset, every attribute this scenario has rows for *or* a current version for
    is reconciled — the second half is what notices an attribute whose last
    override row was deleted, which is a real change and retires the version
    rather than leaving the last digest current forever.

    Bumping only this scenario's partitions is what keeps the baseline out of it.
    The baseline partition of an override asset never has a version, so no edit
    to any scenario can move a baseline result.
    """
    from reimagina.assets.freshness import graph_available
    from reimagina.assets.keys import partition_key, scenario_override_asset_key
    from reimagina.assets.publish import publish_version, retire_version

    if not graph_available(conn):
        return {}
    partition = partition_key({"scenario": scenario_id})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attribute_key,
                   md5(string_agg(
                       feature_id::text || '|' ||
                       coalesce(value_numeric::text, '') || '|' ||
                       coalesce(value_text, ''),
                       E'\\n' ORDER BY feature_id))
            FROM curated_geo.scenario_override
            WHERE scenario_id = %s
            GROUP BY attribute_key
            """,
            (scenario_id,),
        )
        digests = dict(cur.fetchall())
        if attributes is None:
            # Attributes this scenario *had* a version for, so one whose last
            # override row was deleted is reconciled rather than left current.
            cur.execute(
                """
                SELECT a.backing_ref
                FROM curated_geo.asset_version av
                JOIN curated_geo.asset a ON a.id = av.asset_id
                WHERE a.backing_kind = 'scenario_override'
                  AND av.partition_key = %s AND av.is_current
                """,
                (partition,),
            )
            targets = set(digests) | {r[0] for r in cur.fetchall()}
        else:
            targets = set(attributes)

    published: dict[str, str | None] = {}
    for attribute in sorted(targets):
        asset_key = scenario_override_asset_key(attribute)
        digest = digests.get(attribute)
        if digest is None:
            retire_version(conn, asset_key=asset_key, partition_key=partition)
            published[attribute] = None
        else:
            published[attribute] = publish_version(
                conn, asset_key=asset_key, version=digest, partition_key=partition
            )
    return published


def effective_numeric(
    conn: Any,
    *,
    type_id: str,
    attribute_key: str,
    scenario_id: str | None = None,
    feature_ids: list[str] | None = None,
) -> dict[str, float]:
    """``{feature_id: effective value}`` = override ⊕ observation ⊕ base jsonb.

    Only features whose effective value is non-null are returned.
    """
    _require_postgres(conn)
    id_filter = " AND f.id = ANY(%(fids)s::uuid[])" if feature_ids else ""
    params: dict[str, Any] = {
        "key": attribute_key,
        "type_id": type_id,
        "scenario": scenario_id,
    }
    if feature_ids:
        params["fids"] = list(feature_ids)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT f.id::text,
                   COALESCE(
                     so.value_numeric,
                     obs.value_numeric,
                     NULLIF(f.attributes->>%(key)s, '')::double precision
                   )
            FROM curated_geo.feature f
            LEFT JOIN curated_geo.observation obs
                   ON obs.feature_id = f.id
                  AND obs.attribute_key = %(key)s
                  AND obs.is_current
            LEFT JOIN curated_geo.scenario_override so
                   ON so.feature_id = f.id
                  AND so.attribute_key = %(key)s
                  AND so.scenario_id = %(scenario)s
            WHERE f.type_id = %(type_id)s{id_filter}
            """,
            params,
        )
        return {fid: val for fid, val in cur.fetchall() if val is not None}


def scenario_change_set(conn: Any, *, scenario_id: str) -> tuple[set[str], set[str]]:
    """``(changed_attributes, edited_features)`` implied by a scenario's overrides.

    The override rows *are* the diff a scenario run needs, so reading them back
    means a caller need not re-declare what changed.
    """
    _require_postgres(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attribute_key, feature_id::text "
            "FROM curated_geo.scenario_override WHERE scenario_id = %s",
            (scenario_id,),
        )
        rows = cur.fetchall()
    return {a for a, _ in rows}, {f for _, f in rows}


def resolve_change_set(
    conn: Any,
    *,
    changed_attributes: Any,
    edited_features: Any,
    scenario_id: str | None,
) -> tuple[list[str], list[str]]:
    """Effective change-set: the explicit one when given, else derived from the
    scenario's overrides. Baseline (no scenario) always uses the explicit lists.

    ponytail: flat union of (attributes, features) — a safe over-approximation.
    With several overridden attributes a dependent may recompute a feature whose
    specific attribute didn't change; never wrong, just not minimal. Per-attribute
    scoping is the upgrade path, moot while only ``floors`` is overridable.
    """
    changed = list(changed_attributes or [])
    features = list(edited_features or [])
    if changed or not scenario_id:
        return changed, features
    attrs, feats = scenario_change_set(conn, scenario_id=scenario_id)
    return sorted(attrs), sorted(feats)
