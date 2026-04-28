"""Structured exception handlers for the reimagina API.

Three handlers cover the realistic failure modes — split so the HTTP
status code carries useful retry semantics:

- `psycopg2.OperationalError` / `InterfaceError` — the DB is unreachable
  or the connection broke. 503 `Service Unavailable`; client may retry.
- Any other `psycopg2.Error` (ProgrammingError, DataError, IntegrityError,
  …) is a shipped-SQL bug, not a transient condition. 500 `Internal
  Server Error`; retrying won't help.
- `Exception` (catch-all) — anything else becomes 500.

In every case we return a stable JSON shape and a request_id the
operator can correlate with server logs. The internal message is never
echoed back.
"""

from __future__ import annotations

import logging

import psycopg2
from fastapi import Request
from fastapi.responses import JSONResponse

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
