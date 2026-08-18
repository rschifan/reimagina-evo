"""CLI helpers for the first Overture Mexico City source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from apps.cli.indicators import submit_after_ingest
from reimagina.db import connect
from reimagina.pipeline.overture import (
    MEXICO_CITY_BBOX,
    OVERTURE_BUILDINGS_MEXICO_CITY,
    OVERTURE_BUILDINGS_NOTES,
    OVERTURE_BUILDINGS_SOURCE_NAME,
    OVERTURE_DEFAULT_RELEASE,
    bootstrap_overture_buildings_subset,
    extract_overture_buildings_subset,
    validate_overture_buildings_layer,
)


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse ``--bbox xmin,ymin,xmax,ymax`` into a 4-tuple. Lives in the
    CLI module (not the pipeline) so the pipeline keeps a typed input
    contract — the string form is a presentation concern."""
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must be xmin,ymin,xmax,ymax (got {value!r})")
    xmin, ymin, xmax, ymax = (float(p) for p in parts)
    if xmin >= xmax or ymin >= ymax:
        raise ValueError(f"bbox must be ordered xmin<xmax, ymin<ymax (got {value!r})")
    return (xmin, ymin, xmax, ymax)


def mexico_city_buildings_example() -> dict[str, str]:
    """Return a small machine-readable description of the default Overture source."""
    return {
        "name": OVERTURE_BUILDINGS_MEXICO_CITY.name,
        "provider": OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        "version": OVERTURE_BUILDINGS_MEXICO_CITY.version,
        "bbox": ",".join(str(v) for v in OVERTURE_BUILDINGS_MEXICO_CITY.bbox),
    }


def extract_mexico_city_buildings(
    *,
    output_path: str | Path,
    release: str = OVERTURE_DEFAULT_RELEASE,
    bbox: tuple[float, float, float, float] = MEXICO_CITY_BBOX,
) -> dict[str, Any]:
    """Extract an Overture buildings subset to local GeoParquet.

    ``release`` and ``bbox`` are required to support multi-AOI ingest:
    the CLI passes the AOI's bbox so each city pulls only its own
    footprint from S3.
    """
    extracted_path = extract_overture_buildings_subset(
        output_path=output_path, release=release, bbox=bbox
    )
    return {
        "output_path": str(extracted_path),
        "format": "GeoParquet",
        "release": release,
        "bbox": ",".join(str(v) for v in bbox),
    }


def load_mexico_city_buildings(
    *,
    dsn: str,
    raw_path: str | Path,
    orchestrator_run_id: str | None = None,
    code_version: str = "dev",
    release: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
    aoi_id: str | None = None,
    recompute: bool = False,
    recompute_attributes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Bootstrap an Overture buildings extract into Postgres.

    ``aoi_id`` (optional) clips the just-loaded rows to that AOI's
    polygon — required when the bbox is wider than the actual AOI
    (true for every real city extract from a rectangular S3 query).

    ``recompute`` submits an indicator run for the attributes this load
    invalidated. ⚠ **Opt-in, and it stays opt-in.** A city-scale load can
    invalidate every block-grain indicator, and those units are budgeted in
    minutes — ``UNIT_EXECUTION_TIMEOUT`` defaults to two hours. A loader that
    silently started that would make ``overture load`` an unbounded operation
    whose cost is invisible at the call site. The operator asks for it.

    ``recompute_attributes`` is now **empty by default**, and the dirty set is
    derived from freshness instead. The load publishes a new version for the
    layer it wrote, so every indicator consuming that layer reports itself out
    of date without anyone naming an attribute.

    It used to default to ``geometry`` — true of writing building footprints,
    but a proxy rather than the fact: ``geometry`` is read by three indicators,
    so reloading buildings also recomputed compactness, which never looks at a
    building. The layer edge says exactly which indicators are affected, which
    is what the attribute name was standing in for.

    Passing attributes explicitly still works and still wins, for a caller that
    knows something the graph does not.
    """
    with connect(dsn) as conn:
        result = bootstrap_overture_buildings_subset(
            conn,
            raw_path=raw_path,
            orchestrator_run_id=orchestrator_run_id or str(uuid4()),
            code_version=code_version,
            dataset_version=release,
            source_name=source_name,
            notes=notes,
            aoi_id=aoi_id,
        )

    if recompute:
        # ⚠ Nothing written, nothing invalidated. A load whose rows all hit the
        # ON CONFLICT DO NOTHING path changed no geometry, and recomputing a
        # city because a re-run was a no-op is the expensive way to learn that.
        if result.get("records_written"):
            result["recompute"] = submit_after_ingest(
                dsn=dsn,
                changed_attributes=list(recompute_attributes),
                aoi_id=aoi_id,
                code_version=code_version,
            )
        else:
            result["recompute"] = {"status": "skipped", "reason": "no records written"}

    return result


def validate_mexico_city_buildings(*, dsn: str) -> dict[str, Any]:
    """Inspect the serving layer after the Overture buildings load."""
    with connect(dsn) as conn:
        return validate_overture_buildings_layer(conn)


def as_json(data: dict[str, Any]) -> str:
    """Serialize CLI output consistently for humans and scripts."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)
