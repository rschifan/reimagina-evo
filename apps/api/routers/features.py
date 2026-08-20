"""Spatial-feature inventory — thin wrapper over ServingReader.get_features.

The endpoint returns feature IDs + attributes, never geometry. The web app
consumes geometry through Martin's MVT tiles; the API exposes the
attribute/catalog side so list views and search don't have to parse tiles.
"""

from __future__ import annotations

from uuid import UUID

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_backend, get_db_conn

router = APIRouter(prefix="/features", tags=["features"])


class FeatureRow(BaseModel):
    """Serving-layer feature attributes (no geometry in the JSON payload)."""

    id: str
    type_slug: str
    role: str
    name: str | None = None
    attributes: dict | None = None


class FeaturePage(BaseModel):
    """Cursor-paginated page of features."""

    features: list[FeatureRow]
    next_cursor: str | None = None


@router.get("", response_model=FeaturePage)
def list_features(
    type_slug: str | None = Query(default=None),
    role: str | None = Query(default=None),
    # ``UUID``, not ``str``: the column is declared ``uuid``, so a malformed one
    # used to reach Postgres and come back as a DataError the generic handler
    # turns into a 500. A client typo belongs at the boundary, as a 422.
    aoi_id: UUID | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5_000),
    after_cursor: str | None = Query(default=None),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> FeaturePage:
    """Paginated feature inventory — wraps ServingReader.get_features."""
    try:
        payload = (
            get_backend(conn)
            .serving_reader()
            .get_features(
                type_slug=type_slug,
                role=role,
                aoi_id=None if aoi_id is None else str(aoi_id),
                limit=limit,
                after_cursor=after_cursor,
            )
        )
    except ValueError as exc:
        # decode_cursor raises ValueError for malformed tokens. That's a
        # client mistake, not a server fault — surface it as 422 rather
        # than letting the generic 500 handler catch it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FeaturePage(
        features=[_feature_row(row) for row in payload["features"]],
        next_cursor=payload.get("next_cursor"),
    )


def _feature_row(row: dict) -> FeatureRow:
    return FeatureRow(
        id=str(row["id"]),
        type_slug=row["type_slug"],
        role=row["role"],
        name=row.get("name"),
        attributes=row.get("attributes"),
    )
