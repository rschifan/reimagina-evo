"""CLI helpers for the INEGI Marco Geoestadístico 2024 + Censo 2020 ingests.

Thin wrappers around ``reimagina.pipeline.inegi_marco`` and
``reimagina.pipeline.inegi_censo`` matching the shape of
``apps/cli/cdmx.py`` so the broader CLI surface stays consistent.

Census rollups (block→AGEB, block→municipality) live in the generic
``aggregation_recipe`` machinery and are intentionally NOT auto-seeded
from this module. Operators wire them up explicitly when needed —
keeps the prototype free of dead aggregation rows and lets a future
aggregation framework own that decision.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from reimagina.db import managed_connection
from reimagina.pipeline import inegi_censo, inegi_marco


def bootstrap_inegi_marco(
    *,
    dsn: str,
    layers: list[str] | None = None,
    cve_ent_filter: list[str] | None = None,
    source_root: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stage + load the requested INEGI marco layers into curated_geo.feature."""
    with managed_connection(dsn) as conn:
        result = inegi_marco.bootstrap(
            conn,
            layers=layers,
            cve_ent_filter=cve_ent_filter,
            source_root=Path(source_root) if source_root else None,
            code_version=code_version,
        )
    return {"dsn": dsn, **result}


def bootstrap_inegi_censo(
    *,
    dsn: str,
    cve_ent_filter: list[str] | None = None,
    columns: list[str] | None = None,
    source_root: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stream the INEGI 2020 census subset into curated_geo.observation,
    keyed by CVEGEO against the existing block features."""
    with managed_connection(dsn) as conn:
        result = inegi_censo.bootstrap(
            conn,
            cve_ent_filter=cve_ent_filter,
            columns=tuple(columns) if columns else inegi_censo.HEADLINE_COLUMNS,
            source_root=Path(source_root) if source_root else None,
            code_version=code_version,
        )
    return {"dsn": dsn, **dataclasses.asdict(result)}
