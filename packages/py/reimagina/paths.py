"""Canonical filesystem locations for the reimagina package.

Replaces the ``Path(__file__).resolve().parents[N]`` + ``REIMAGINA_DATA_DIR``
resolution that was copy-pasted across the pipeline, tiles, and migration
modules. Import these instead of re-deriving the repo root per module.
"""

from __future__ import annotations

import os
from pathlib import Path

# This module lives at packages/py/reimagina/paths.py, so the repo root is
# three levels up. Centralising it here means callers never depend on their own
# nesting depth.
REPO_ROOT = Path(__file__).resolve().parents[3]


def data_dir() -> Path:
    """Base data directory. Override with the ``REIMAGINA_DATA_DIR`` env var
    (e.g. to point at a mounted volume); defaults to ``<repo>/data``."""
    return Path(os.environ.get("REIMAGINA_DATA_DIR", str(REPO_ROOT / "data")))
