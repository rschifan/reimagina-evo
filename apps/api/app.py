"""FastAPI application factory for the reimagina read-side.

The API exposes curated indicator results, AOI summaries, spatial-feature
inventories, and the resolved tile-source manifest. It never owns its own
SQL — every route calls into `reimagina.storage` repositories, keeping the
architectural invariant that domain packages don't import app code but the
API freely imports domain code.

Run locally with:
    python -m apps.api
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterable

import psycopg2
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from rio_tiler.errors import PointOutsideBounds
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import TilerFactory

from . import deps, errors
from .cog_security import validated_cog_path
from .middleware import RequestIdMiddleware
from .routers import aois, features, health, indicators, jobs, metrics, mvt, tiles


def _cors_origins_from_env() -> list[str]:
    """Parse REIMAGINA_API_CORS_ORIGINS (comma-separated) or fall back to dev."""
    raw = os.environ.get("REIMAGINA_API_CORS_ORIGINS")
    if raw is None:
        return ["http://localhost:5173", "http://localhost:8001"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _log_resolved_dsn() -> None:
    """Log the API's resolved Postgres DSN at startup so operators
    catch DB-mismatch bugs immediately (Codex review HIGH finding).

    The MVT path goes API + Martin → both must point at the same
    Postgres database. We can't read Martin's DSN, but printing the
    API's at startup gives operators a reliable cross-check.

    Strips the password component before logging so credentials don't
    leak into logfiles.
    """
    import logging
    from reimagina.storage import mask_dsn
    from . import deps

    logger = logging.getLogger("reimagina.api")
    raw = deps.api_dsn()
    sanitised = mask_dsn(raw)
    logger.info(
        "API connected to Postgres DSN: %s — verify Martin is on the "
        "same DB or MVT tiles will be silently empty.",
        sanitised,
    )


def create_app(
    *,
    allow_origins: Iterable[str] | None = None,
    initialise_pool: bool = True,
) -> FastAPI:
    """Build a FastAPI app with routers, middleware, and exception handlers.

    `allow_origins=None` reads from REIMAGINA_API_CORS_ORIGINS (comma-
    separated) and falls back to the dev hosts. Tests can force a specific
    list in-process.

    `initialise_pool=False` skips the lifespan hook that creates the
    process-wide Postgres pool — useful for unit tests that exercise the
    app factory without a live database.
    """
    resolved_origins = (
        list(allow_origins) if allow_origins is not None else _cors_origins_from_env()
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if initialise_pool:
            deps.initialise_pool()
            _log_resolved_dsn()
        try:
            yield
        finally:
            if initialise_pool:
                deps.shutdown_pool()

    app = FastAPI(
        title="reimagina API",
        version="0.1.0",
        description=(
            "Read-side for the reimagina urban-planning platform. "
            "Exposes curated indicator results, AOI summaries, spatial "
            "feature inventories, and the tile source manifest."
        ),
        lifespan=lifespan,
    )

    # Order matters: request-id needs to wrap everything so exception
    # handlers can pick it off `request.state`. CORS wraps request-id so
    # preflight requests still carry the id on the response.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
        allow_credentials=False,
    )

    # Order matters: FastAPI walks handlers most-specific → least-specific,
    # so the OperationalError/InterfaceError pair must register *before*
    # the generic psycopg2.Error handler. Otherwise a connectivity outage
    # would wrongly surface as 500 instead of 503.
    app.add_exception_handler(
        psycopg2.OperationalError, errors.database_unavailable_handler
    )
    app.add_exception_handler(
        psycopg2.InterfaceError, errors.database_unavailable_handler
    )
    app.add_exception_handler(psycopg2.Error, errors.database_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)

    app.include_router(health.router)
    app.include_router(indicators.router)
    app.include_router(aois.router)
    app.include_router(features.router)
    app.include_router(metrics.router)
    app.include_router(tiles.router)
    # Live vector tiles from this process, behind the same URL contract
    # Martin serves. Mounted unconditionally and *not* the default: which of
    # the two a client is sent to is `REIMAGINA_TILE_LIVE_BASE_URL`, resolved
    # by `tiles.sources`, so this route existing costs nothing until a
    # deployment points at it. See `apps/api/routers/mvt.py`, and
    # `docs/superpowers/specs/API_SERVED_MVT.md` for why both exist.
    app.include_router(mvt.router)
    app.include_router(jobs.router)
    # Raster tiles: mount titiler's standard COG endpoints under /cog so
    # the API + tile server share a process, a dep tree, and an origin.
    # Splitting titiler into a sidecar (separate uvicorn or Docker) is
    # a later scaling decision — until then, one less moving part.
    #
    # path_dependency: titiler's default accepts ANY ``url`` query param
    # and hands it straight to rasterio/GDAL, which would let a client
    # read arbitrary host files via ``/cog/info?url=file:///etc/passwd``.
    # ``validated_cog_path`` pins the URL to file:// under
    # ``host_data_root()`` so only registered COGs are servable.
    cog = TilerFactory(path_dependency=validated_cog_path)
    app.include_router(cog.router, prefix="/cog", tags=["raster-tiles"])
    # Map rio_tiler errors to the right HTTP status codes. Without
    # these, panning past a raster's footprint floods the console with
    # 500s — and browsers report 500s on cross-origin tile requests as
    # "blocked by CORS policy" because Starlette's outermost error
    # middleware skips the CORS-header pass on uncaught exceptions.
    # 404 is handled silently by MapLibre as "tile unavailable"; same
    # status for point reads keeps the picker tooltip from spamming
    # the console when the cursor is outside the active raster.
    # titiler's DEFAULT_STATUS_CODES omits PointOutsideBounds — pin it
    # to 404 explicitly.
    add_exception_handlers(app, {**DEFAULT_STATUS_CODES, PointOutsideBounds: 404})
    return app


app = create_app()
