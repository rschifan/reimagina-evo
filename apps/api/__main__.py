"""Entry-point for `python -m apps.api`.

Boots uvicorn with the FastAPI instance created in `apps.api.app`. The port
and host default to dev-friendly values but respect REIMAGINA_API_HOST /
REIMAGINA_API_PORT env vars for container / prod use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure `reimagina.*` is importable when the API is launched as
# `python -m apps.api` from a clean checkout without an editable install.
_PACKAGES_DIR = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

import uvicorn  # noqa: E402

from .app import app as _app  # noqa: E402


def main() -> None:
    uvicorn.run(
        _app,
        host=os.environ.get("REIMAGINA_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("REIMAGINA_API_PORT", "8001")),
        log_level=os.environ.get("REIMAGINA_API_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
