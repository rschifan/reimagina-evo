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

from collections.abc import Iterable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import aois, features, health, indicators, tiles


DEFAULT_DEV_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",   # Vite dev server
    "http://localhost:8000",   # API's own OpenAPI page
)


def create_app(
    *,
    allow_origins: Iterable[str] = DEFAULT_DEV_ORIGINS,
) -> FastAPI:
    """Build a FastAPI app with all read-side routers mounted.

    Kept as a factory (rather than a module-level singleton) so tests can
    construct isolated app instances with their own CORS / DSN overrides
    without touching a global.
    """
    app = FastAPI(
        title="reimagina API",
        version="0.1.0",
        description=(
            "Read-side for the reimagina urban-planning platform. "
            "Exposes curated indicator results, AOI summaries, spatial "
            "feature inventories, and the tile source manifest."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allow_origins),
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    app.include_router(health.router)
    app.include_router(indicators.router)
    app.include_router(aois.router)
    app.include_router(features.router)
    app.include_router(tiles.router)
    return app


app = create_app()
