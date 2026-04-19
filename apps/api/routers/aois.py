"""AOI listing + cached summary read endpoints."""
from __future__ import annotations

from datetime import datetime

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_backend, get_db_conn

router = APIRouter(prefix="/aois", tags=["aois"])


class AOISummary(BaseModel):
    """Shape of an aoi_indicator_summary row, API-facing."""

    aoi_id: str
    scenario_id: str | None = None
    indicator_id: str
    value: float | None = None
    aggregation_note: str | None = None
    status: str
    computed_at: datetime


class AOIRow(BaseModel):
    """Minimal AOI record. Geometry omitted — the web app fetches tiles instead."""

    aoi_id: str
    project_id: str
    name: str
    description: str | None = None


@router.get("", response_model=list[AOIRow])
def list_aois(
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[AOIRow]:
    """Every AOI registered in the current database.

    Reads curated_geo.area_of_interest directly rather than through a repo
    because AOI listing has no first-class Protocol method yet. Kept small
    so promoting it to AOIRepository later is a one-line change.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, project_id::text, name, description
            FROM curated_geo.area_of_interest
            ORDER BY name
            """
        )
        rows = cur.fetchall()
    return [
        AOIRow(aoi_id=aid, project_id=pid, name=name, description=descr)
        for aid, pid, name, descr in rows
    ]


@router.get("/{aoi_id}/summaries", response_model=list[AOISummary])
def list_summaries(
    aoi_id: str,
    scenario_id: str | None = Query(default=None),
    only_current: bool = Query(default=True),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[AOISummary]:
    """Cached aoi_indicator_summary rows for one AOI."""
    _assert_aoi_exists(conn, aoi_id)
    rows = get_backend(conn).aoi_summary_repo().list_summaries(
        aoi_id=aoi_id,
        scenario_id=scenario_id,
        only_current=only_current,
    )
    return [
        AOISummary(
            aoi_id=str(row["aoi_id"]),
            scenario_id=str(row["scenario_id"]) if row.get("scenario_id") else None,
            indicator_id=str(row["indicator_id"]),
            value=row.get("value"),
            aggregation_note=row.get("aggregation_note"),
            status=row["status"],
            computed_at=row["computed_at"],
        )
        for row in rows
    ]


def _assert_aoi_exists(conn, aoi_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM curated_geo.area_of_interest WHERE id = %s",
            (aoi_id,),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"Unknown AOI id: {aoi_id!r}")
