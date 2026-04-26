"""AOI listing + cached summary read endpoints."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

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
    """Minimal AOI record. Geometry omitted — the web app fetches tiles instead.

    ``bbox`` is ``[min_lon, min_lat, max_lon, max_lat]`` in EPSG:4326,
    derived from ``ST_Envelope`` so the multi-site web picker can fly
    the map to an AOI without a separate geometry round-trip. ``None``
    when the AOI was inserted without a geometry (rare, but possible
    in fixtures).
    """

    aoi_id: str
    project_id: str
    name: str
    description: str | None = None
    bbox: list[float] | None = None


@router.get("", response_model=list[AOIRow])
def list_aois(
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[AOIRow]:
    """Every AOI registered in the current database."""
    rows = get_backend(conn).aoi_repo().list_aois()
    return [AOIRow(**row) for row in rows]


@router.get("/{aoi_id}/summaries", response_model=list[AOISummary])
def list_summaries(
    aoi_id: UUID,
    scenario_id: UUID | None = Query(default=None),
    only_current: bool = Query(default=True),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[AOISummary]:
    """Cached aoi_indicator_summary rows for one AOI."""
    aoi_id_str = str(aoi_id)
    scenario_id_str = str(scenario_id) if scenario_id is not None else None
    _assert_aoi_exists(conn, aoi_id_str)
    rows = get_backend(conn).aoi_summary_repo().list_summaries(
        aoi_id=aoi_id_str,
        scenario_id=scenario_id_str,
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
    if get_backend(conn).aoi_repo().get_aoi(aoi_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown AOI id: {aoi_id!r}")
