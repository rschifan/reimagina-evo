"""Scenario overrides + effective attribute resolution — the what-if mechanism.

A scenario's attribute edits live in ``curated_geo.scenario_override``. The
effective value of an attribute for a feature under a scenario is
``override ⊕ current observation ⊕ base feature.attributes`` (first non-null
wins). Baseline (``scenario_id=None``) collapses to ``observation ⊕ base``.

Postgres-only — matches the async/orchestration Postgres path; DuckDB is frozen.
The impact engine needs no change: an override that edits ``attribute_key`` is
exactly the ``changed_attributes`` token it already keys the dirty-set off.
"""

from __future__ import annotations

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
) -> None:
    """Upsert one scenario attribute edit (the what-if)."""
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
