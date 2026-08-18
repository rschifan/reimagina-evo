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
import reimagina.pipeline.cdmx as cdmx_pipeline
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


def aoi_from_state(
    *,
    dsn: str,
    cve_ent: str,
    name: str,
    project_name: str = "CDMX Realistic-Scale Validation",
) -> dict[str, Any]:
    """Create an AOI whose geometry is the matching MEX:state polygon.

    Same shape as ``aoi_from_alcaldia`` but for an entire entidad rather
    than a single municipality. Useful when the operator wants 'all of
    CDMX' (CVE_ENT=09) or 'all of Baja California' (CVE_ENT=02) as a
    single AOI rather than per-municipality rows.

    No-op if the state feature isn't loaded yet — emits a status the
    caller can act on.
    """
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        row = _fetch_state_wkt(backend, cve_ent=cve_ent)
        if row is None:
            return {
                "dsn": dsn,
                "status": "missing_state",
                "cve_ent": cve_ent,
                "message": (
                    f"No MEX:state feature with CVE_ENT={cve_ent} is loaded. "
                    f"Run `inegi-marco bootstrap --cve-ent {cve_ent} "
                    f"--layers areas_estatales` first."
                ),
            }

        state_id, geometry_wkt = row
        project_id = _ensure_dev_project(backend, project_name)
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name=name,
            geometry_wkt=geometry_wkt,
            description=(f"AOI derived from MEX:state CVE_ENT={cve_ent} ({name})."),
            # State scope needs only the entity code; every municipality, AGEB
            # and block inside it carries the same CVE_ENT.
            membership_key={"CVE_ENT": cve_ent},
        )
        conn.commit()

    return {
        "dsn": dsn,
        "status": "ok",
        "aoi_id": aoi_id,
        "project_id": project_id,
        "cve_ent": cve_ent,
        "state_feature_id": str(state_id),
        "name": name,
    }


def aoi_from_alcaldia(
    *,
    dsn: str,
    cve_mun: str,
    name: str,
    cve_ent: str = "09",
    project_name: str = "CDMX Realistic-Scale Validation",
) -> dict[str, Any]:
    """Create an AOI whose geometry is the matching MEX:municipality polygon.

    Looks up the municipality feature by (CVE_ENT, CVE_MUN), reads its
    WKT, and calls aoi.create_aoi under a dedicated dev project. Intended
    for quick scoping of city-scale operations (overlap compute, tile
    exports, benchmarks). No-op if the municipality isn't loaded yet —
    emits a status message the caller can act on.

    Despite the name, this works for any Mexican state: pass ``cve_ent``
    to target Baja California (``02``), CDMX (``09`` — default), etc.
    The ``alcaldia`` shorthand persists because CDMX was the first pilot.
    """
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        row = _fetch_municipality_wkt(backend, cve_ent=cve_ent, cve_mun=cve_mun)
        if row is None:
            return {
                "dsn": dsn,
                "status": "missing_municipality",
                "cve_ent": cve_ent,
                "cve_mun": cve_mun,
                "message": (
                    f"No MEX:municipality feature with CVE_ENT={cve_ent} and "
                    f"CVE_MUN={cve_mun} is loaded. For CDMX run "
                    f"`cdmx bootstrap --layers municipalities`; for other states "
                    f"run `inegi-marco bootstrap --cve-ent {cve_ent} --layers areas_municipales`."
                ),
            }

        municipality_id, geometry_wkt = row
        project_id = _ensure_dev_project(backend, project_name)
        aoi_id = aoi_mod.create_aoi(
            conn,
            project_id=project_id,
            name=name,
            geometry_wkt=geometry_wkt,
            description=(
                f"AOI derived from MEX:municipality "
                f"CVE_ENT={cve_ent} CVE_MUN={cve_mun} ({name})."
            ),
            # The same codes the description has always carried in prose, now
            # in a form compute_overlaps can act on (r040). A block is in this
            # alcaldia because INEGI says so, not because its polygon overlaps.
            membership_key={"CVE_ENT": cve_ent, "CVE_MUN": cve_mun},
        )
        conn.commit()

    return {
        "dsn": dsn,
        "status": "ok",
        "aoi_id": aoi_id,
        "project_id": project_id,
        "cve_ent": cve_ent,
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


_DEV_PROJECT_DESCRIPTION = "Dev project for CDMX realistic-scale validation."


def _fetch_municipality_wkt(
    backend,
    *,
    cve_ent: str,
    cve_mun: str,
) -> tuple[str, str] | None:
    """Return (feature_id, geometry_wkt) for the municipality matching (CVE_ENT, CVE_MUN)."""
    row = backend.feature_repo().find_by_attributes(
        type_slug="municipality",
        attributes={"CVE_ENT": cve_ent, "CVE_MUN": cve_mun},
    )
    if row is None:
        return None
    return row["id"], row["geometry_wkt"]


def _fetch_state_wkt(backend, *, cve_ent: str) -> tuple[str, str] | None:
    """Return (feature_id, geometry_wkt) for the state matching CVE_ENT.

    INEGI marco_geostadistico ships exactly one ``MEX:state`` row per
    entidad — the entire CDMX outline for CVE_ENT=09, all of BC for
    CVE_ENT=02, etc."""
    row = backend.feature_repo().find_by_attributes(
        type_slug="state",
        attributes={"CVE_ENT": cve_ent},
    )
    if row is None:
        return None
    return row["id"], row["geometry_wkt"]


def _ensure_dev_project(backend, name: str) -> str:
    """Idempotently create (or reuse) an app_core.project row by name. Returns its id."""
    return backend.aoi_repo().upsert_project_by_name(name, _DEV_PROJECT_DESCRIPTION)
