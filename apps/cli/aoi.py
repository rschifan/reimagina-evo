"""CLI helpers for area-of-interest lifecycle operations.

The cdmx-flavoured creators (``aoi_from_alcaldia`` / ``aoi_from_state``)
live in ``apps/cli/cdmx.py`` because they read MEX-specific feature
rows. This module hosts the AOI-agnostic operations: list and delete.

Why a top-level ``aoi`` group rather than extending ``cdmx``: deletion
is read-only on cdmx data and applies to any AOI regardless of how it
was created. Keeping it under cdmx would mislead operators into
thinking there's a separate cdmx-only delete path.
"""

from __future__ import annotations

from typing import Any

from reimagina.data.aoi import compute_aoi_features
from reimagina.db import managed_connection


def compute_features(*, dsn: str, aoi_id: str | None = None) -> dict[str, Any]:
    """(Re)populate ``curated_geo.aoi_feature`` for one or all AOIs.

    Idempotent — ``compute_aoi_features`` deletes existing rows for the
    target AOI first. Bootstrap step 6 used to be inline Python; this
    promotes it to a CLI subcommand so the runbook stays uniformly
    ``apps.cli.app …`` and the operator can re-run a single AOI without
    rebuilding the whole matrix."""
    with managed_connection(dsn) as conn:
        cur = conn.cursor()
        if aoi_id is None:
            cur.execute(
                "SELECT id::text, name FROM curated_geo.area_of_interest ORDER BY name"
            )
            targets = cur.fetchall()
        else:
            cur.execute(
                "SELECT id::text, name FROM curated_geo.area_of_interest WHERE id = %s",
                (aoi_id,),
            )
            targets = cur.fetchall()
            if not targets:
                return {"dsn": dsn, "status": "not_found", "aoi_id": aoi_id}
        results = []
        for row_id, name in targets:
            n = compute_aoi_features(conn, row_id)
            conn.commit()
            results.append({"aoi_id": row_id, "name": name, "rows_inserted": n})
    return {"dsn": dsn, "status": "ok", "aois": results}


def list_aois(*, dsn: str) -> dict[str, Any]:
    """Return every AOI row with bbox + project name. Used as the
    operator's first stop when reasoning about what's in the DB."""
    with managed_connection(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id::text, a.name, p.name AS project_name,
                   ST_XMin(a.geometry), ST_YMin(a.geometry),
                   ST_XMax(a.geometry), ST_YMax(a.geometry),
                   (SELECT count(*) FROM curated_geo.aoi_feature af
                    WHERE af.aoi_id = a.id) AS feature_count,
                   a.created_at
            FROM curated_geo.area_of_interest a
            LEFT JOIN app_core.project p ON p.id = a.project_id
            ORDER BY a.name
            """
        )
        rows = cur.fetchall()
    return {
        "dsn": dsn,
        "aois": [
            {
                "id": r[0],
                "name": r[1],
                "project": r[2],
                "bbox": [r[3], r[4], r[5], r[6]],
                "feature_count": r[7],
                "created_at": str(r[8]),
            }
            for r in rows
        ],
    }


def delete_aoi(*, dsn: str, aoi_id: str) -> dict[str, Any]:
    """Delete an AOI by uuid. CASCADE on aoi_feature handles cleanup of
    the per-AOI overlap rows; features themselves are not touched
    because they're shared across AOIs (and may stay in the DB for a
    different AOI's coverage). Returns ``{status: 'deleted', ...}`` on
    success or ``{status: 'not_found'}`` if the uuid wasn't present."""
    with managed_connection(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM curated_geo.area_of_interest WHERE id = %s", (aoi_id,)
        )
        row = cur.fetchone()
        if row is None:
            return {"dsn": dsn, "status": "not_found", "aoi_id": aoi_id}
        name = row[0]
        cur.execute(
            "SELECT count(*) FROM curated_geo.aoi_feature WHERE aoi_id = %s",
            (aoi_id,),
        )
        cascaded_features = cur.fetchone()[0]
        cur.execute("DELETE FROM curated_geo.area_of_interest WHERE id = %s", (aoi_id,))
        conn.commit()
    return {
        "dsn": dsn,
        "status": "deleted",
        "aoi_id": aoi_id,
        "name": name,
        "cascaded_aoi_feature_rows": cascaded_features,
    }
