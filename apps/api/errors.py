"""Structured exception handlers for the reimagina API.

Two handlers cover the realistic failure modes:

- `psycopg2.Error` — the DB is unreachable / query blew up. Responds 503
  `Service Unavailable` with a stable JSON body so clients can decide
  whether to retry.
- `Exception` (catch-all) — anything else becomes 500 with the same
  JSON shape. We deliberately do not echo the internal message — the
  request_id lets the operator correlate the user-visible error with
  server logs without leaking stack trace text through the API.
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


async def psycopg_error_handler(request: Request, exc: psycopg2.Error) -> JSONResponse:
    """DB-level failure → 503 with a structured body."""
    request_id = _request_id(request)
    log.warning(
        "psycopg error on %s %s (request_id=%s): %s",
        request.method, request.url.path, request_id, exc,
    )
    return _error_response(
        status_code=503,
        error_code="database_unavailable",
        message="The database was unreachable or rejected the query.",
        request_id=request_id,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything not already routed → 500. Server log keeps the details."""
    request_id = _request_id(request)
    log.exception(
        "unhandled error on %s %s (request_id=%s)",
        request.method, request.url.path, request_id,
    )
    return _error_response(
        status_code=500,
        error_code="internal_error",
        message="An unexpected error occurred. See server logs with the given request_id.",
        request_id=request_id,
    )
