"""CLI helpers for the per-pilot-city amenity / POI ingest.

Thin wrappers around ``reimagina.pipeline.points`` matching the shape
of ``apps/cli/inegi.py`` so the CLI surface stays consistent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reimagina.db import connect
from reimagina.pipeline import points


def bootstrap_points(
    *,
    dsn: str,
    layers: list[str] | None = None,
    source_root: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stage + load the requested amenity layers into curated_geo.feature."""
    with connect(dsn) as conn:
        result = points.bootstrap(
            conn,
            layers=layers,
            source_root=Path(source_root) if source_root else None,
            code_version=code_version,
        )
    return {"dsn": dsn, **result}
