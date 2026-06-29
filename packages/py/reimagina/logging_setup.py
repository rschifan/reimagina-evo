"""Process-wide logging configuration.

A single idempotent entrypoint so diagnostics from ``reimagina.*`` loggers are
visible with consistent formatting. The API delegates log configuration to
uvicorn; the CLI calls this so its warnings reach **stderr** — stdout stays the
machine-readable JSON result channel.
"""

from __future__ import annotations

import logging
import os
import sys


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, emitting to stderr.

    Level defaults to the ``REIMAGINA_LOG_LEVEL`` env var, else ``INFO``.
    Idempotent: ``logging.basicConfig`` is a no-op once a handler exists, so
    calling this from multiple entrypoints is safe.
    """
    logging.basicConfig(
        level=level or os.environ.get("REIMAGINA_LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
