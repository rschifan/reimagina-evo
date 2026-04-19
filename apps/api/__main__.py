"""Entry-point for `python -m apps.api`.

Boots uvicorn with the FastAPI instance created in `apps.api.app`. The port
and host default to dev-friendly values but respect REIMAGINA_API_HOST /
REIMAGINA_API_PORT env vars for container / prod use.
"""
from __future__ import annotations

import os

import uvicorn

from .app import app as _app


def main() -> None:
    uvicorn.run(
        _app,
        host=os.environ.get("REIMAGINA_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("REIMAGINA_API_PORT", "8000")),
        log_level=os.environ.get("REIMAGINA_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
