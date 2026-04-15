"""CLI helpers for the first Overture Mexico City source."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from reimagina.db import connect
from reimagina.pipeline.overture import (
    OVERTURE_BUILDINGS_MEXICO_CITY,
    bootstrap_overture_buildings_subset,
    extract_overture_buildings_subset,
    overture_buildings_extract_sql,
    validate_overture_buildings_layer,
)


def mexico_city_buildings_example() -> dict[str, str]:
    """Return a small machine-readable description of the first Overture source."""
    return {
        "name": OVERTURE_BUILDINGS_MEXICO_CITY.name,
        "provider": OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        "version": OVERTURE_BUILDINGS_MEXICO_CITY.version,
        "bbox": ",".join(str(v) for v in OVERTURE_BUILDINGS_MEXICO_CITY.bbox),
        "extract_sql": overture_buildings_extract_sql(),
    }


def print_extract_sql() -> dict[str, str]:
    """Return the DuckDB SQL used to build the local GeoParquet subset."""
    return {"extract_sql": overture_buildings_extract_sql()}


def extract_mexico_city_buildings(
    *,
    output_path: str | Path,
) -> dict[str, Any]:
    """Extract the Mexico City buildings subset to local GeoParquet via DuckDB."""
    extracted_path = extract_overture_buildings_subset(output_path=output_path)
    return {
        "output_path": str(extracted_path),
        "format": "GeoParquet",
        "version": OVERTURE_BUILDINGS_MEXICO_CITY.version,
        "bbox": ",".join(str(v) for v in OVERTURE_BUILDINGS_MEXICO_CITY.bbox),
    }


def load_mexico_city_buildings(
    *,
    dsn: str,
    raw_path: str | Path,
    orchestrator_run_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Run the first-source bootstrap flow against the provided database."""
    with connect(dsn) as conn:
        return bootstrap_overture_buildings_subset(
            conn,
            raw_path=raw_path,
            orchestrator_run_id=orchestrator_run_id or str(uuid4()),
            code_version=code_version,
        )


def validate_mexico_city_buildings(*, dsn: str) -> dict[str, Any]:
    """Inspect the serving layer after the Overture buildings load."""
    with connect(dsn) as conn:
        return validate_overture_buildings_layer(conn)


def as_json(data: dict[str, Any]) -> str:
    """Serialize CLI output consistently for humans and scripts."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)
