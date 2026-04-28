"""Cross-cutting HTTP middleware for the reimagina API.

`request_id_middleware` propagates a stable correlation id:

- If the incoming request carries `X-Request-Id`, that value is used.
- Otherwise a fresh UUID4 is generated.

The id is attached to `request.state.request_id` so handlers and
exception handlers can log / surface it, and is echoed back in the
response headers for downstream systems to pick up.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Ensure every request carries a stable correlation id."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or str(uuid.uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
