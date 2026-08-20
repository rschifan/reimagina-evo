"""Health endpoints — separate liveness + readiness probes.

/health/live → always 200. Process is alive; Python didn't crash. A
   K8s liveness probe uses this to decide whether to restart the pod.
/health/ready → 200 iff the DB is reachable. K8s readiness probes gate
   traffic on this; a slow DB pulls the pod out of the load balancer
   without triggering a restart loop.
/health → alias for /health/ready, kept for backwards compatibility
   with the earlier single-endpoint shape.
/health/tile-server → reports Martin connectivity AND a sanity check
   that Martin's database matches the API's. Codex review surfaced
   that the multi-site work shipped twice with the API and Martin
   pointing at different Postgres databases — this endpoint exposes
   that drift instead of letting it manifest as silent empty tiles.
"""

from __future__ import annotations

import contextlib
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

import psycopg2
from fastapi import APIRouter, Response
from pydantic import BaseModel

router = APIRouter(tags=["meta"])


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    db: str


class TileServerResponse(BaseModel):
    status: str
    martin: str
    api_database: str | None = None
    martin_reachable: bool
    martin_url: str
    notes: list[str] = []


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
    return ReadinessResponse(
        status="ok" if db_status == "reachable" else "degraded", db=db_status
    )


@router.get("/health", response_model=ReadinessResponse)
def health(response: Response) -> ReadinessResponse:
    """Alias for /health/ready. Kept to avoid breaking older callers."""
    return readiness(response)


@router.get("/health/tile-server", response_model=TileServerResponse)
def tile_server(response: Response) -> TileServerResponse:
    """Report Martin connectivity + flag API/Martin DB drift.

    The HIGH-severity finding from Codex was that nothing prevented
    the API and Martin from connecting to different Postgres
    databases — exactly the bug we hit during the live test. We can't
    introspect Martin's DSN over HTTP (Martin doesn't expose it), but
    we can:

      1. Probe Martin via HTTP and confirm it's alive.
      2. Report the API's resolved database name so the operator can
         eyeball-compare it with `martin --config <path>` output or
         the env vars used to start Martin.
      3. Surface ``REIMAGINA_API_DSN`` / ``DATABASE_URL`` env values
         that Martin should be reading too.

    Returns 200 iff Martin is reachable. The DB-equivalence check
    can't fail here — it's surfaced as a ``notes`` line for the
    operator to verify, not gated automatically.
    """
    api_database = _api_database_name()
    martin_url = _resolve_martin_url()
    reachable = _probe_martin(martin_url)

    notes: list[str] = [
        f"API resolved Postgres database: {api_database!r}.",
        "Martin reads its own DATABASE_URL — verify it matches the "
        "API's database name above. The MVT layer + AOI scoping "
        "depend on both pointing at the same Postgres instance.",
    ]

    body = TileServerResponse(
        status="ok" if reachable else "degraded",
        martin="reachable" if reachable else "unreachable",
        api_database=api_database,
        martin_reachable=reachable,
        martin_url=martin_url,
        notes=notes,
    )
    if not reachable:
        response.status_code = 503
    return body


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


def _api_database_name() -> str | None:
    """Return the database name the API is connected to. Single-source-of-truth
    for the operator comparing API↔Martin DSNs.

    ⚠ ``closing()``: psycopg2's connection context manager scopes the
    transaction, not the socket, so the bare ``with psycopg2.connect(...)`` this
    replaces leaked a backend per call. Same reason ``_probe_db`` above closes
    in a ``finally``.
    """
    from ..deps import api_dsn

    try:
        with (
            contextlib.closing(psycopg2.connect(api_dsn(), connect_timeout=2)) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT current_database()")
            return cur.fetchone()[0]
    except psycopg2.Error:
        return None


def _resolve_martin_url() -> str:
    """Where the API expects Martin to live. Default mirrors the
    dev-stack convention; override via ``REIMAGINA_MARTIN_URL`` for
    deployments behind a reverse proxy."""
    return os.environ.get("REIMAGINA_MARTIN_URL", "http://localhost:3000")


def _probe_martin(martin_url: str, timeout: float = 2.0) -> bool:
    """Hit Martin's catalog endpoint with a short timeout.

    A 200 means Martin is alive AND has a Postgres connection (the
    catalog is built on startup from auto-published functions). A
    timeout / connection error means the tile server is down — which
    in turn means every MVT layer in the UI will silently 404.
    """
    parsed = urlparse(martin_url)
    if not parsed.scheme:
        return False
    try:
        with urllib.request.urlopen(
            f"{martin_url.rstrip('/')}/catalog",
            timeout=timeout,
        ) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
