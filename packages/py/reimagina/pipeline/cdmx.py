"""CDMX canonical-geometry bootstrap: registers, stages, and loads INEGI + CDMX layers.

Mirrors the Overture precedent (`pipeline/overture.py`): each layer is a frozen
dataclass spec consumed by generic stage/load helpers that ultimately call
`pipeline.loader.load_vector_source`. No inline SQL, no backend sniffing.

The four supported layers cover the fine-grain → coarse-grain MEX hierarchy
we need for cross-grain indicator aggregation:

- `municipalities` — 16 CDMX alcaldías (role=context)
- `agebs`          — ~2.4k urban AGEBs  (role=analysis_unit)
- `blocks`         — ~62k manzanas      (role=analysis_unit)
- `colonias`       — ~1.8k neighborhoods (role=context)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.storage import backend_for_connection

from . import bronze, inegi, silver
from .loader import load_vector_source


CDMX_PROVIDER_INEGI = "INEGI"
CDMX_PROVIDER_CDMX = "Gobierno de la Ciudad de México"
CDMX_LICENSE_INEGI = "Libre uso — Términos de Uso de la Información del INEGI"
CDMX_LICENSE_CDMX = "Licencia CDMX Open Data"
CDMX_MARCO_VERSION = "2020.1"
CDMX_COLONIAS_VERSION = "2024.open"


@dataclass(frozen=True)
class CDMXLayerSpec:
    """Describes one CDMX canonical layer end-to-end.

    Fields chosen so stage + load are fully driven by the spec — no
    per-layer branching in the caller.
    """

    key: str
    display_name: str
    provider: str
    version: str
    license: str | None
    feature_type_context: str
    feature_type_slug: str
    source_record_ref_col: str
    name_col: str | None
    attributes_cols: tuple[str, ...]
    portal_url: str
    notes: str
    archive_filename: str
    shp_suffix: str | None  # None for non-zip (direct GeoJSON) sources
    download_url: str
    # For shapefile sources, `archive_filename` is the zip; `shp_suffix`
    # picks the target layer out of the archive. For GeoJSON sources,
    # `archive_filename` is the .geojson and `shp_suffix` is None.


CDMX_MUNICIPALITIES = CDMXLayerSpec(
    key="municipalities",
    display_name="CDMX Alcaldías (INEGI Marco Geoestadístico)",
    provider=CDMX_PROVIDER_INEGI,
    version=CDMX_MARCO_VERSION,
    license=CDMX_LICENSE_INEGI,
    feature_type_context="MEX",
    feature_type_slug="municipality",
    source_record_ref_col="CVEGEO",
    name_col="NOMGEO",
    attributes_cols=("CVE_ENT", "CVE_MUN", "NOMGEO"),
    portal_url=inegi.INEGI_PORTAL_URL,
    notes="16 alcaldías (municipios) of Ciudad de México from INEGI 2020.",
    archive_filename="09_ciudaddemexico.zip",
    shp_suffix="09mun.shp",
    download_url=inegi.INEGI_MARCO_CDMX_URL,
)

CDMX_AGEBS = CDMXLayerSpec(
    key="agebs",
    display_name="CDMX AGEBs urbanos (INEGI Marco Geoestadístico)",
    provider=CDMX_PROVIDER_INEGI,
    version=CDMX_MARCO_VERSION,
    license=CDMX_LICENSE_INEGI,
    feature_type_context="MEX",
    feature_type_slug="ageb",
    source_record_ref_col="CVEGEO",
    name_col=None,
    attributes_cols=("CVE_ENT", "CVE_MUN", "CVE_LOC", "CVE_AGEB"),
    portal_url=inegi.INEGI_PORTAL_URL,
    notes="Urban AGEBs for Ciudad de México from INEGI Marco Geoestadístico 2020.",
    archive_filename="09_ciudaddemexico.zip",
    shp_suffix="09a.shp",
    download_url=inegi.INEGI_MARCO_CDMX_URL,
)

CDMX_BLOCKS = CDMXLayerSpec(
    key="blocks",
    display_name="CDMX Manzanas (INEGI Marco Geoestadístico)",
    provider=CDMX_PROVIDER_INEGI,
    version=CDMX_MARCO_VERSION,
    license=CDMX_LICENSE_INEGI,
    feature_type_context="MEX",
    feature_type_slug="block",
    source_record_ref_col="CVEGEO",
    name_col=None,
    attributes_cols=("CVE_ENT", "CVE_MUN", "CVE_LOC", "CVE_AGEB", "CVE_MZA"),
    portal_url=inegi.INEGI_PORTAL_URL,
    notes="Manzanas (city blocks) for Ciudad de México from INEGI Marco Geoestadístico 2020.",
    archive_filename="09_ciudaddemexico.zip",
    shp_suffix="09m.shp",
    download_url=inegi.INEGI_MARCO_CDMX_URL,
)

CDMX_COLONIAS = CDMXLayerSpec(
    key="colonias",
    display_name="CDMX Colonias (Portal de Datos Abiertos CDMX)",
    provider=CDMX_PROVIDER_CDMX,
    version=CDMX_COLONIAS_VERSION,
    license=CDMX_LICENSE_CDMX,
    feature_type_context="MEX",
    feature_type_slug="neighborhood",
    source_record_ref_col="cve_col",
    name_col="nomgeo",
    attributes_cols=("cve_col", "nomgeo", "alcaldia"),
    portal_url=inegi.CDMX_COLONIAS_PORTAL_URL,
    notes="Colonias de la Ciudad de México, capa oficial del Gobierno de CDMX.",
    archive_filename="cdmx_colonias.geojson",
    shp_suffix=None,
    download_url=inegi.CDMX_COLONIAS_URL,
)

LAYER_SPECS: dict[str, CDMXLayerSpec] = {
    spec.key: spec for spec in (
        CDMX_MUNICIPALITIES,
        CDMX_AGEBS,
        CDMX_BLOCKS,
        CDMX_COLONIAS,
    )
}

# Load order: municipalities first so `--alcaldia` filtering of the
# fine-grain layers can key off `CVE_MUN`. Colonias last because they're
# from a different source and don't depend on the others.
DEFAULT_LOAD_ORDER: tuple[str, ...] = ("municipalities", "agebs", "blocks", "colonias")


# ─── Fetch ─────────────────────────────────────────────────────────────────

def fetch_layer(spec: CDMXLayerSpec, *, overwrite: bool = False) -> Path:
    """Download (or reuse cached) archive for a CDMX layer. Returns the local path."""
    result = inegi.cached_download(
        spec.download_url,
        dest_dir=inegi.CDMX_SOURCES_DIR,
        filename=spec.archive_filename,
        portal_url=spec.portal_url,
        overwrite=overwrite,
    )
    return result.path


def resolve_source_file(spec: CDMXLayerSpec, archive_path: Path) -> Path:
    """Given a downloaded archive, return the concrete file we'll land to bronze.

    For shapefile sources we extract the zip beside it and return the target
    .shp file. For direct GeoJSON the archive path *is* the source file.
    """
    if spec.shp_suffix is None:
        return archive_path
    extract_dir = archive_path.parent / archive_path.stem
    inegi.extract_zip(archive_path, extract_dir)
    return inegi.find_shapefile(extract_dir, spec.shp_suffix)


# ─── Source registration + stage ───────────────────────────────────────────

def ensure_layer_source(conn, spec: CDMXLayerSpec) -> str:
    """Idempotently register the data_source_catalog row for a CDMX layer."""
    existing = cat.get_source(
        conn,
        name=spec.display_name,
        provider=spec.provider,
        version=spec.version,
    )
    if existing is not None:
        return str(existing["id"])
    return cat.register_source(
        conn,
        name=spec.display_name,
        provider=spec.provider,
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri=f"pending://cdmx/{spec.key}",
        version=spec.version,
        license=spec.license,
        notes=spec.notes,
    )


def stage_layer(
    spec: CDMXLayerSpec,
    source_file: Path,
    *,
    source_id: str,
) -> tuple[Path, Path]:
    """Land source_file to bronze, read + clean to silver. Returns (bronze, silver).

    Shapefiles ship as a family (.shp + .shx + .dbf + .prj + .cpg + …); all
    sidecars next to the target .shp are landed together so Silver can open
    the bronze copy without reaching back into the sources cache.
    """
    bronze_path = bronze.land_file(source_file, source_id)
    if source_file.suffix.lower() == ".shp":
        for sidecar in source_file.parent.iterdir():
            if sidecar == source_file or sidecar.stem != source_file.stem:
                continue
            if not sidecar.is_file():
                continue
            bronze.land_file(sidecar, source_id)
    gdf = silver.read_vector(bronze_path)
    silver_filename = f"{spec.key}_silver.parquet"
    silver_path = silver.save_silver_vector(gdf, source_id, silver_filename)
    return bronze_path, silver_path


# ─── Load ──────────────────────────────────────────────────────────────────

def load_layer(
    conn,
    spec: CDMXLayerSpec,
    *,
    source_id: str,
    silver_path: Path,
    orchestrator_run_id: str,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Load a staged silver GeoParquet into curated_geo.feature via load_vector_source."""
    type_id = feat.get_type_id(conn, spec.feature_type_context, spec.feature_type_slug)
    return load_vector_source(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        type_id=type_id,
        dataset_version=spec.version,
        source_record_ref_col=spec.source_record_ref_col,
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
        name_col=spec.name_col,
        attributes_cols=list(spec.attributes_cols),
    )


# ─── End-to-end bootstrap + status ─────────────────────────────────────────

def bootstrap_layers(
    conn,
    *,
    layers: list[str] | None = None,
    from_paths: dict[str, Path] | None = None,
    orchestrator_run_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Fetch (or reuse cached) + stage + load each layer, in DEFAULT_LOAD_ORDER.

    `layers` restricts the set; defaults to all four.
    `from_paths` lets callers override the download — maps layer key to a local
    pre-fetched archive path (useful when INEGI URLs are gated or flaky).
    """
    requested = list(layers) if layers else list(DEFAULT_LOAD_ORDER)
    unknown = [k for k in requested if k not in LAYER_SPECS]
    if unknown:
        raise ValueError(f"Unknown CDMX layer key(s): {unknown}")
    ordered = [k for k in DEFAULT_LOAD_ORDER if k in requested]

    run_id = orchestrator_run_id or str(uuid4())
    from_paths = from_paths or {}

    feat.seed_feature_types(conn)
    conn.commit()

    per_layer: list[dict[str, Any]] = []
    for key in ordered:
        spec = LAYER_SPECS[key]
        archive_path = from_paths.get(key) or fetch_layer(spec)
        source_file = resolve_source_file(spec, archive_path)

        source_id = ensure_layer_source(conn, spec)
        conn.commit()

        bronze_path, silver_path = stage_layer(spec, source_file, source_id=source_id)
        cat.update_source_storage_uri(conn, source_id, bronze.bronze_uri(source_id, bronze_path.name))
        conn.commit()

        load_result = load_layer(
            conn,
            spec,
            source_id=source_id,
            silver_path=silver_path,
            orchestrator_run_id=run_id,
            code_version=code_version,
        )
        per_layer.append({
            "layer": key,
            "type_slug": spec.feature_type_slug,
            "source_id": source_id,
            "bronze_path": str(bronze_path),
            "silver_path": str(silver_path),
            **load_result,
        })

    return {
        "orchestrator_run_id": run_id,
        "layers": per_layer,
        "layers_loaded": [row["layer"] for row in per_layer],
    }


def layer_status(conn) -> dict[str, int]:
    """Return row counts per MEX feature type_slug in curated_geo.feature.

    Uses the feature_type_stats view so it works on both backends without
    raw SQL here.
    """
    backend = backend_for_connection(conn)
    stats = backend.explore_reader().list_feature_stats()
    totals: dict[str, int] = {}
    for row in stats:
        slug = row.get("type_slug")
        if slug is None:
            continue
        totals[slug] = totals.get(slug, 0) + int(row.get("row_count", 0))
    return totals
