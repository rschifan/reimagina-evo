"""Indicator catalog + current-values read endpoints.

Download formats (CSV, GeoJSON, GeoParquet) live in a separate
commit — this module only ships JSON list / detail / value endpoints.
"""
from __future__ import annotations

import json
from datetime import datetime

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ..deps import get_backend, get_db_conn
from ..exports import (
    indicator_rows_to_csv,
    indicator_rows_to_geojson,
    indicator_rows_to_geoparquet,
)

router = APIRouter(prefix="/indicators", tags=["indicators"])


class IndicatorSummary(BaseModel):
    """One row of curated_geo.indicator_catalog, API-facing shape."""

    slug: str
    name: str
    description: str | None = None
    model_family: str | None = None
    computation_kind: str
    value_type: str
    unit: str | None = None
    valid_min: float | None = None
    valid_max: float | None = None
    aggregation_method: str
    applies_to: list[str]


class IndicatorValue(BaseModel):
    """One row of serving_geo.current_indicator_v, API-facing shape."""

    feature_id: str
    feature_type: str
    indicator_slug: str
    scenario_id: str | None = None
    value_numeric: float | None = None
    value_text: str | None = None
    value_json: dict | list | None = None
    quality_flag: str
    confidence: float | None = None
    computed_at: datetime
    unit: str | None = None
    indicator_name: str | None = None


@router.get("", response_model=list[IndicatorSummary])
def list_indicators(
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[IndicatorSummary]:
    """Every indicator currently registered in the catalog."""
    rows = get_backend(conn).indicator_catalog_repo().list_indicators()
    return [IndicatorSummary(**_catalog_row_to_dict(row)) for row in rows]


@router.get("/{slug}", response_model=IndicatorSummary)
def get_indicator(
    slug: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> IndicatorSummary:
    """One indicator's full catalog metadata, by slug."""
    row = get_backend(conn).indicator_catalog_repo().get_indicator(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown indicator slug: {slug!r}")
    return IndicatorSummary(**_catalog_row_to_dict(row))


@router.get("/{slug}/values", response_model=list[IndicatorValue])
def list_values(
    slug: str,
    scenario_id: str | None = Query(default=None, description="Optional scenario filter; base city if omitted"),
    limit: int = Query(default=1_000, ge=1, le=10_000),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> list[IndicatorValue]:
    """Current indicator results, one row per feature.

    Pagination for prototype is a simple `limit`. The table caps at ~66k
    rows per indicator in CDMX, so a client fetching the full set does
    two or three round-trips; cursor pagination is deferred.
    """
    repo = get_backend(conn).indicator_catalog_repo()
    if repo.get_indicator(slug) is None:
        raise HTTPException(status_code=404, detail=f"Unknown indicator slug: {slug!r}")

    rows = get_backend(conn).indicator_result_repo().list_current_for_indicator(
        indicator_slug=slug,
        scenario_id=scenario_id,
    )
    return [IndicatorValue(**_value_row_to_dict(row)) for row in rows[:limit]]


@router.get("/{slug}/values.csv", response_class=StreamingResponse)
def download_values_csv(
    slug: str,
    scenario_id: str | None = Query(default=None),
    feature_type: str | None = Query(default=None),
    limit: int = Query(default=100_000, ge=1, le=500_000),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> StreamingResponse:
    """Stream a CSV of current indicator values.

    Narrow columns (feature_id, type_slug, value, quality_flag,
    computed_at) so consumers scripting against the file don't break on
    schema additions — richer fields belong to the GeoJSON / GeoParquet
    downloads.
    """
    _ensure_indicator_exists(conn, slug)
    rows = get_backend(conn).indicator_result_repo().list_current_with_geometry(
        indicator_slug=slug,
        scenario_id=scenario_id,
        feature_type=feature_type,
        limit=limit,
    )
    filename = f"{slug}.csv"
    return StreamingResponse(
        indicator_rows_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{slug}/values.geojson")
def download_values_geojson(
    slug: str,
    scenario_id: str | None = Query(default=None),
    feature_type: str | None = Query(default=None),
    limit: int = Query(default=100_000, ge=1, le=500_000),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> Response:
    """Return a GeoJSON FeatureCollection in one response."""
    _ensure_indicator_exists(conn, slug)
    rows = get_backend(conn).indicator_result_repo().list_current_with_geometry(
        indicator_slug=slug,
        scenario_id=scenario_id,
        feature_type=feature_type,
        limit=limit,
    )
    payload = indicator_rows_to_geojson(rows)
    filename = f"{slug}.geojson"
    return Response(
        content=json.dumps(payload),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{slug}/values.parquet")
def download_values_parquet(
    slug: str,
    scenario_id: str | None = Query(default=None),
    feature_type: str | None = Query(default=None),
    limit: int = Query(default=100_000, ge=1, le=500_000),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> Response:
    """Return a GeoParquet byte payload (EPSG:4326)."""
    _ensure_indicator_exists(conn, slug)
    rows = get_backend(conn).indicator_result_repo().list_current_with_geometry(
        indicator_slug=slug,
        scenario_id=scenario_id,
        feature_type=feature_type,
        limit=limit,
    )
    payload = indicator_rows_to_geoparquet(rows)
    filename = f"{slug}.parquet"
    return Response(
        content=payload,
        media_type="application/vnd.apache.parquet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _ensure_indicator_exists(conn, slug: str) -> None:
    if get_backend(conn).indicator_catalog_repo().get_indicator(slug) is None:
        raise HTTPException(status_code=404, detail=f"Unknown indicator slug: {slug!r}")


def _catalog_row_to_dict(row: dict) -> dict:
    """Normalise the RealDictCursor/tuple-dict shape into kwargs for IndicatorSummary."""
    return {
        "slug": row["slug"],
        "name": row["name"],
        "description": row.get("description"),
        "model_family": row.get("model_family"),
        "computation_kind": row["computation_kind"],
        "value_type": row["value_type"],
        "unit": row.get("unit"),
        "valid_min": row.get("valid_min"),
        "valid_max": row.get("valid_max"),
        "aggregation_method": row["aggregation_method"],
        "applies_to": list(row.get("applies_to") or []),
    }


def _value_row_to_dict(row: dict) -> dict:
    """Normalise a current_indicator_v row to IndicatorValue kwargs."""
    return {
        "feature_id": str(row["feature_id"]),
        "feature_type": row["feature_type"],
        "indicator_slug": row["indicator_slug"],
        "scenario_id": str(row["scenario_id"]) if row.get("scenario_id") else None,
        "value_numeric": row.get("value_numeric"),
        "value_text": row.get("value_text"),
        "value_json": row.get("value_json"),
        "quality_flag": row["quality_flag"],
        "confidence": row.get("confidence"),
        "computed_at": row["computed_at"],
        "unit": row.get("unit"),
        "indicator_name": row.get("indicator_name"),
    }
