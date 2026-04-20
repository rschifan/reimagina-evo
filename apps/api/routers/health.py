"""Health endpoints — separate liveness + readiness probes.

/health/live → always 200. Process is alive; Python didn't crash. A
   K8s liveness probe uses this to decide whether to restart the pod.
/health/ready → 200 iff the DB is reachable. K8s readiness probes gate
   traffic on this; a slow DB pulls the pod out of the load balancer
   without triggering a restart loop.
/health → alias for /health/ready, kept for backwards compatibility
   with the earlier single-endpoint shape.
"""
from __future__ import annotations

import psycopg2
from fastapi import APIRouter, Response
from pydantic import BaseModel

router = APIRouter(tags=["meta"])


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    db: str


@router.get("/health/live", response_model=LivenessResponse)
def liveness() -> LivenessResponse:
    """Process-alive probe. Never touches the database."""
    return LivenessResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
def readiness(response: Response) -> ReadinessResponse:
    """Readiness probe. 200 if the DB is reachable; 503 otherwise."""
    db_status = _probe_db()
    if db_status != "reachable":
        response.status_code = 503
    return ReadinessResponse(status="ok" if db_status == "reachable" else "degraded", db=db_status)


@router.get("/health", response_model=ReadinessResponse)
def health(response: Response) -> ReadinessResponse:
    """Alias for /health/ready. Kept to avoid breaking older callers."""
    return readiness(response)


def _probe_db() -> str:
    """Open a fresh connection outside the pool to avoid exhausting it on probes.

    Using get_db_conn would consume a pool slot on every probe. Since
    readiness probes fire every few seconds from load balancers, we open
    a dedicated connection each time — if that fails, readiness fails,
    which is exactly the signal the probe is there to give.
    """
    from ..deps import api_dsn
    try:
        conn = psycopg2.connect(api_dsn(), connect_timeout=2)
    except psycopg2.Error:
        return "unreachable"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            ok = cur.fetchone()[0] == 1
        return "reachable" if ok else "unreachable"
    except psycopg2.Error:
        return "unreachable"
    finally:
        conn.close()
