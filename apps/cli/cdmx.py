"""CLI helpers for the CDMX canonical-geometry bootstrap (INEGI + datos.cdmx.gob.mx).

Mirrors the shape of `apps/cli/overture.py`: thin wrappers that call into
`reimagina.pipeline.cdmx` and return JSON-serialisable payloads. The CLI
surface is intentionally small: fetch, bootstrap, status, and an
aoi-from-alcaldia convenience command.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reimagina.data import aoi as aoi_mod
from reimagina.db import connect
from reimagina.pipeline import cdmx as cdmx_pipeline
from reimagina.pipeline.inegi import InegiFetchError
from reimagina.storage import backend_for_connection


def fetch_cdmx_layer(*, layer: str, overwrite: bool = False) -> dict[str, Any]:
    """Download (or reuse cached) archive for one CDMX layer."""
    spec = _lookup_spec(layer)
    try:
        path = cdmx_pipeline.fetch_layer(spec, overwrite=overwrite)
    except InegiFetchError as exc:
        return {
            "layer": layer,
            "status": "failed",
            "portal_url": spec.portal_url,
            "message": str(exc),
        }
    return {
        "layer": layer,
        "status": "ok",
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "portal_url": spec.portal_url,
    }


def bootstrap_cdmx(
    *,
    dsn: str,
    layers: list[str] | None = None,
    from_paths: dict[str, str] | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Fetch + stage + load the requested CDMX layers into curated_geo.feature."""
    resolved_paths = {k: Path(v) for k, v in (from_paths or {}).items()}
    with connect(dsn) as conn:
        result = cdmx_pipeline.bootstrap_layers(
            conn,
            layers=layers,
            from_paths=resolved_paths,
            code_version=code_version,
        )
    return {"dsn": dsn, **result}


def cdmx_status(*, dsn: str) -> dict[str, Any]:
    """Row counts per MEX feature type in curated_geo.feature."""
    with connect(dsn) as conn:
        counts = cdmx_pipeline.layer_status(conn)
    return {
        "dsn": dsn,
        "counts": counts,
        "ready": all(counts.get(k, 0) > 0 for k in ("municipality", "ageb", "block")),
    }


def aoi_from_alcaldia(
    *,
    dsn: str,
    cve_mun: str,
    name: str,
    project_name: str = "CDMX Realistic-Scale Validation",
) -> dict[str, Any]:
    """Create an AOI whose geometry is the matching MEX:municipality polygon.

    Looks up the municipality feature by (CVE_ENT=09, CVE_MUN=<cve_mun>), reads
    its WKT, and calls aoi.create_aoi under a dedicated dev project. Intended
    for quick scoping of city-scale operations (overlap compute, tile exports,
    benchmarks). No-op if the municipality isn't loaded yet — emits a status
    message the caller can act on.
    """
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        row = _fetch_municipality_wkt(backend, cve_mun=cve_mun)
        if row is None:
            return {
                "dsn": dsn,
                "status": "missing_municipality",
                "cve_mun": cve_mun,
                "message": (
                    f"No MEX:municipality feature with CVE_ENT=09 and CVE_MUN={cve_mun} "
                    f"is loaded. Run `cdmx bootstrap --layers municipalities` first."
                ),
            }

        municipality_id, geometry_wkt = row
        project_id = _ensure_dev_project(backend, project_name)
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name=name,
            geometry_wkt=geometry_wkt,
            description=f"AOI derived from MEX:municipality {cve_mun} ({name}).",
        )
        conn.commit()

    return {
        "dsn": dsn,
        "status": "ok",
        "aoi_id": aoi_id,
        "project_id": project_id,
        "cve_mun": cve_mun,
        "municipality_feature_id": str(municipality_id),
        "name": name,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────


def _lookup_spec(layer: str) -> cdmx_pipeline.CDMXLayerSpec:
    try:
        return cdmx_pipeline.LAYER_SPECS[layer]
    except KeyError as exc:
        raise ValueError(
            f"Unknown CDMX layer {layer!r}; expected one of "
            f"{sorted(cdmx_pipeline.LAYER_SPECS)}"
        ) from exc


def _fetch_municipality_wkt(backend, *, cve_mun: str) -> tuple[str, str] | None:
    """Return (feature_id, geometry_wkt) for the CDMX municipality with the given CVE_MUN.

    Kept backend-aware in one place; both DuckDB and Postgres answer the same
    query with the same ST_AsText / attributes JSON indexing semantics.
    """
    conn = backend.connection
    if backend.name == "postgres":
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, ST_AsText(geometry)
                FROM curated_geo.feature
                WHERE type_slug = 'municipality'
                  AND attributes->>'CVE_ENT' = '09'
                  AND attributes->>'CVE_MUN' = %s
                LIMIT 1
                """,
                (cve_mun,),
            )
            row = cur.fetchone()
    else:
        row = conn.execute(
            """
            SELECT CAST(id AS VARCHAR), ST_AsText(geometry)
            FROM curated_geo.feature
            WHERE type_slug = 'municipality'
              AND json_extract_string(attributes, '$.CVE_ENT') = '09'
              AND json_extract_string(attributes, '$.CVE_MUN') = ?
            LIMIT 1
            """,
            [cve_mun],
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _ensure_dev_project(backend, name: str) -> str:
    """Idempotently create (or reuse) an app_core.project row by name. Returns its id."""
    conn = backend.connection
    if backend.name == "postgres":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text FROM app_core.project WHERE name = %s LIMIT 1",
                (name,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return str(existing[0])
            cur.execute(
                "INSERT INTO app_core.project (name, description) VALUES (%s, %s) RETURNING id::text",
                (name, "Dev project for CDMX realistic-scale validation."),
            )
            return str(cur.fetchone()[0])

    existing = conn.execute(
        "SELECT CAST(id AS VARCHAR) FROM app_core.project WHERE name = ? LIMIT 1",
        [name],
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    # DuckDB: no RETURNING for this schema; generate UUID client-side.
    from uuid import uuid4

    project_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO app_core.project (id, name, description)
        VALUES (?, ?, ?)
        """,
        [project_id, name, "Dev project for CDMX realistic-scale validation."],
    )
    return project_id
