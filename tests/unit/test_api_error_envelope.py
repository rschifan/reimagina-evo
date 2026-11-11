"""The API's own error envelope must survive every handler registration.

`apps.api.errors` promises a stable JSON shape and that "the internal message
is never echoed back". That promise is only kept if `unhandled_error_handler`
is the handler Starlette actually reaches for an otherwise-unhandled
exception — and `create_app` registers two catch-alls, ours and the one
titiler ships inside `DEFAULT_STATUS_CODES`.
"""

from __future__ import annotations

from fastapi import FastAPI

from apps.api import errors
from apps.api.app import create_app


def test_unhandled_exceptions_use_this_apps_handler():
    """titiler's DEFAULT_STATUS_CODES maps `Exception` to a handler that
    returns ``{"detail": str(exc)}``. Registering it last replaces ours —
    `add_exception_handler` is a dict assignment — so every unhandled error on
    every route starts echoing the exception's text back to the client: SQL,
    bound parameters, and whatever a driver chose to put in the message. It
    also drops the request id the envelope exists to carry.
    """
    app: FastAPI = create_app()
    assert app.exception_handlers.get(Exception) is errors.unhandled_error_handler


def test_titilers_raster_specific_handlers_are_still_registered():
    """The fix must remove *only* the catch-all. titiler's per-error codes are
    the reason `add_exception_handlers` is called at all, and dropping them
    would turn a tile outside bounds back into a 500.
    """
    from rio_tiler.errors import TileOutsideBounds

    app: FastAPI = create_app()
    assert TileOutsideBounds in app.exception_handlers
