"""Structured exception handlers for the API.

Three handlers cover the realistic failure modes — split so the HTTP
status code carries useful retry semantics:

- `psycopg2.OperationalError` / `InterfaceError` — the DB is unreachable
  or the connection broke. 503 `Service Unavailable`; client may retry.
- Any other `psycopg2.Error` (ProgrammingError, DataError, IntegrityError,
  …) is a shipped-SQL bug, not a transient condition. 500 `Internal
  Server Error`; retrying won't help.
- `Exception` (catch-all) — anything else becomes 500.

`OpsPlaneNotProvisioned` is handled here too, and is the one exception to the
rule below about never echoing the internal message — see its handler.

In every case we return a stable JSON shape and a request_id the
operator can correlate with server logs. The internal message is never
echoed back.
"""

from __future__ import annotations

import logging

import psycopg2
from fastapi import Request
from fastapi.responses import JSONResponse

from reimagina.orchestration.port import OpsPlaneNotProvisioned

from .middleware import REQUEST_ID_HEADER


log = logging.getLogger("reimagina.api")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(
    *,
    status_code: int,
    error_code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": error_code,
                "message": message,
                "request_id": request_id,
            }
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


async def database_unavailable_handler(
    request: Request, exc: psycopg2.OperationalError | psycopg2.InterfaceError
) -> JSONResponse:
    """Connection-class failures → 503. Retrying can help."""
    request_id = _request_id(request)
    log.warning(
        "database unavailable on %s %s (request_id=%s): %s",
        request.method,
        request.url.path,
        request_id,
        exc,
    )
    return _error_response(
        status_code=503,
        error_code="database_unavailable",
        message="The database was unreachable.",
        request_id=request_id,
    )


async def database_error_handler(request: Request, exc: psycopg2.Error) -> JSONResponse:
    """Non-connection DB errors (ProgrammingError, DataError, …) → 500.

    These are shipped-SQL bugs or constraint violations that the client
    can't recover from by retrying. The 503 path is reserved for
    transient conditions so operators and clients can distinguish them.
    """
    request_id = _request_id(request)
    log.exception(
        "database error on %s %s (request_id=%s)",
        request.method,
        request.url.path,
        request_id,
    )
    return _error_response(
        status_code=500,
        error_code="database_error",
        message="A database error occurred. See server logs with the given request_id.",
        request_id=request_id,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything not already routed → 500. Server log keeps the details."""
    request_id = _request_id(request)
    log.exception(
        "unhandled error on %s %s (request_id=%s)",
        request.method,
        request.url.path,
        request_id,
    )
    return _error_response(
        status_code=500,
        error_code="internal_error",
        message="An unexpected error occurred. See server logs with the given request_id.",
        request_id=request_id,
    )


async def ops_plane_not_provisioned_handler(
    request: Request, exc: OpsPlaneNotProvisioned
) -> JSONResponse:
    """A missing ops-plane schema → 503. A deployment state, not a fault.

    `DBOSClient` — what an API process submits through in the default `client`
    execution mode — never runs the engine's schema migrations; only a worker's
    `launch()` creates them. So a database migrated by Alembic alone serves
    every read in this API and rejects every submit, and "500, see the logs"
    sends an operator hunting a code defect that does not exist. 503 says the
    true thing: the service is not ready, the client did nothing wrong, and
    retrying after the remedy will work.

    **Registered app-wide rather than caught per route, which is the fix.**
    Three routes reach `submit_run` — `POST /jobs`, `POST /jobs/{id}/rerun` and
    `POST /scenarios/{id}/recompute` — and only the first translated this. The
    other two answered 500 for the identical condition with the identical
    remedy, so the client was told to look at the logs depending on which
    button it had pressed. One handler covers every present and future submit
    path; a route cannot forget it.

    ⚠ **`str(exc)` is echoed, and it is the only handler here that does so.**
    This module's rule exists because a driver's message carries SQL, bound
    parameters and whatever else the backend put in it. This message is not
    that: it is this project's own sentence, raised by
    `reimagina.orchestration.port`, naming the command that fixes the problem.
    Withholding it would leave the operator with a bare 503 and nothing to act
    on, which is the whole value of the status code here.

    `log.warning`, not `log.exception`: a provisioning step that was skipped has
    no stack worth reading, and a traceback per submit would say a defect was
    found when none was.
    """
    request_id = _request_id(request)
    log.warning(
        "ops plane not provisioned on %s %s (request_id=%s): %s",
        request.method,
        request.url.path,
        request_id,
        exc,
    )
    return _error_response(
        status_code=503,
        error_code="ops_plane_not_provisioned",
        message=str(exc),
        request_id=request_id,
    )
