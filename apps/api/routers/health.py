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

import logging
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

import psycopg2
from fastapi import APIRouter, Response
from pydantic import BaseModel

router = APIRouter(tags=["meta"])

log = logging.getLogger("reimagina.api")


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    db: str
    orchestrator: str = "unknown"
    """Whether this API can actually get work executed.

    ⚠ Reported separately from `db`, because they fail for different reasons
    and send an operator to different places: `db` degraded is a database
    problem, `orchestrator` degraded is either the engine being unreachable or
    a rollout that has not finished deploying workers.
    """


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
    """Readiness probe. 200 when this API can serve *and* get work executed.

    ⚠ It probed only the database, which made "ready" a claim about the wrong
    half of the system. On this track the API executes nothing: it hands runs to
    an engine, and a run submitted with no worker registered for the current
    graph is accepted into a queue nothing will drain. Reporting ready in that
    state routes traffic to an instance that can take work and not do it.

    This is what makes the graph-version check a guarantee rather than a race:
    `submit_run` refuses a run with no worker for its shape, and readiness fails
    for the same reason, so a half-rolled deployment is taken out of the load
    balancer instead of accepting work it will reject one request at a time.
    """
    db_status = _probe_db()
    orchestrator_status = _probe_orchestrator()
    ready = db_status == "reachable" and orchestrator_status == "ready"
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ok" if ready else "degraded",
        db=db_status,
        orchestrator=orchestrator_status,
    )


def _probe_orchestrator() -> str:
    """`ready` | `no_worker_for_graph` | `unreachable` | `mode_not_deployable`.

    Distinct strings rather than a bool: "the engine is down" and "nobody
    deployed a worker for this graph" are both 503 and are fixed by completely
    different people.

    ⚠ Fails **closed**, unlike the admission check in `submit_run`. The
    asymmetry is deliberate — refusing to serve traffic is recoverable and a
    load balancer will route elsewhere, whereas refusing an already-planned
    submission over a transient listing blip discards real work.
    """
    from reimagina.orchestration.factory import DEFAULT_MODE, DURABLE_MODES

    try:
        mode = os.environ.get("REIMAGINA_ORCHESTRATOR") or DEFAULT_MODE
        if mode not in DURABLE_MODES:
            # ⚠ Not "not applicable" — this is a misconfiguration, and the
            # word should say so. `get_orchestrator` refuses a non-durable mode
            # as a deployment default, so an API in this state 500s every
            # request; reporting it as an engine outage would send someone to
            # look at a healthy engine.
            return "mode_not_deployable"

        from reimagina.orchestration import hatchet_app
        from reimagina.orchestration.hatchet_orchestrator import _live_worker_for

        workflow = hatchet_app.workflow_name()
        hatchet_app.get_hatchet().workflows.list()  # engine reachability
    except Exception:  # noqa: BLE001
        log.warning("orchestrator readiness probe failed", exc_info=True)
        return "unreachable"

    # ⚠ Live worker here, mere registration at submit. Readiness failing takes
    # this instance out of rotation, which is recoverable; refusing a submission
    # over a momentarily-busy worker destroys that request's work.
    return "ready" if _live_worker_for(workflow) else "no_worker_for_graph"


@router.get("/health", response_model=ReadinessResponse)
def health(response: Response) -> ReadinessResponse:
    """Legacy alias — **database only**, deliberately not the full readiness.

    ⚠ It delegated to `readiness()`, which changed this endpoint's meaning under
    the very callers the docstring says it exists not to break. Once readiness
    required the orchestrator, an engine outage made `/health` 503 on every
    replica at once — pulling `/indicators`, `/tiles` and every other read path
    out of the load balancer despite them being served entirely from Postgres,
    and putting the pods in a restart loop anywhere this is wired to a liveness
    probe, which is a common choice for a path named `/health`.

    New deployments should use `/health/ready`, which answers the question this
    API actually needs answered: can it serve *and* get work executed.
    """
    db_status = _probe_db()
    if db_status != "reachable":
        response.status_code = 503
    return ReadinessResponse(
        status="ok" if db_status == "reachable" else "degraded",
        db=db_status,
        orchestrator="not_probed",
    )


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
    for the operator comparing API↔Martin DSNs."""
    from ..deps import api_dsn

    try:
        with psycopg2.connect(api_dsn(), connect_timeout=2) as conn:
            with conn.cursor() as cur:
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
        with urllib.request.urlopen(  # noqa: S310 — internal URL
            f"{martin_url.rstrip('/')}/catalog",
            timeout=timeout,
        ) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
