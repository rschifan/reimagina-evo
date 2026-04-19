"""Health-check endpoint — basic liveness + DB probe."""
from __future__ import annotations

import psycopg2
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import get_db_conn

router = APIRouter(tags=["meta"])


class HealthResponse(BaseModel):
    status: str
    db: str


@router.get("/health", response_model=HealthResponse)
def health(
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> HealthResponse:
    """Liveness probe that verifies the DB is reachable at request time."""
    db_status = "unreachable"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone()[0] == 1:
                db_status = "reachable"
    except psycopg2.Error:
        db_status = "unreachable"
    return HealthResponse(status="ok", db=db_status)
