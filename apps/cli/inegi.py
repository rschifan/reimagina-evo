"""CLI helpers for the INEGI Marco Geoestadístico 2024 ingest (data lake source).

Thin wrappers around ``reimagina.pipeline.inegi_marco`` matching the shape
of ``apps/cli/cdmx.py`` so the broader CLI surface stays consistent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reimagina.db import connect
from reimagina.pipeline import inegi_marco


def bootstrap_inegi_marco(
    *,
    dsn: str,
    layers: list[str] | None = None,
    cve_ent_filter: list[str] | None = None,
    source_root: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stage + load the requested INEGI marco layers into curated_geo.feature."""
    with connect(dsn) as conn:
        result = inegi_marco.bootstrap(
            conn,
            layers=layers,
            cve_ent_filter=cve_ent_filter,
            source_root=Path(source_root) if source_root else None,
            code_version=code_version,
        )
    return {"dsn": dsn, **result}
