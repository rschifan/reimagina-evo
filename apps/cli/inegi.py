"""CLI helpers for the INEGI Marco Geoestadístico 2024 + Censo 2010 ingests.

Thin wrappers around ``reimagina.pipeline.inegi_marco``,
``reimagina.pipeline.inegi_censo``, and
``reimagina.pipeline.inegi_censo_rollups`` matching the shape of
``apps/cli/cdmx.py`` so the broader CLI surface stays consistent.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from reimagina.db import connect
from reimagina.pipeline import inegi_censo, inegi_censo_rollups, inegi_marco


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


def bootstrap_inegi_censo(
    *,
    dsn: str,
    cve_ent_filter: list[str] | None = None,
    cve_mun_filter: list[str] | None = None,
    columns: list[str] | None = None,
    source_root: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stream the INEGI 2010 census subset into curated_geo.observation,
    keyed by CVEGEO against the existing block features."""
    with connect(dsn) as conn:
        result = inegi_censo.bootstrap(
            conn,
            cve_ent_filter=cve_ent_filter,
            cve_mun_filter=cve_mun_filter,
            columns=tuple(columns) if columns else inegi_censo.HEADLINE_COLUMNS,
            source_root=Path(source_root) if source_root else None,
            code_version=code_version,
        )
    return {"dsn": dsn, **dataclasses.asdict(result)}


def apply_inegi_censo_rollups(
    *,
    dsn: str,
    columns: list[str] | None = None,
    target_grains: list[str] | None = None,
) -> dict[str, Any]:
    """Seed + run AGEB/municipality rollup recipes for the headline
    census columns. Idempotent on (target, attribute_key_prefix)."""
    cols = tuple(columns) if columns else inegi_censo_rollups.HEADLINE_COLUMNS
    grains = tuple(target_grains) if target_grains else inegi_censo_rollups.TARGET_GRAINS
    with connect(dsn) as conn:
        report = inegi_censo_rollups.apply(
            conn,
            columns=cols,
            target_grains=grains,
            direct_dsn=dsn,
        )
    return {
        "dsn": dsn,
        "columns": list(cols),
        "target_grains": list(grains),
        **dataclasses.asdict(report),
    }
