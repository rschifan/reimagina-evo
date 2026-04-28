"""Ingest point-geometry POI / amenity GeoParquet files into curated_geo.feature.

Targets the per-pilot-city amenity layers downloaded from the Azure data
lake:

  - data/sources/azure/sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet
  - data/sources/azure/exe/002_mexicali/equipamientos.geoparquet

Both share the same column set —
``id, category, capacity, geometry, fuente, fecha, entidad`` — and store
WGS-84 point geometries natively (real GeoParquet, not BLOB), so the
read step is just ``geopandas.read_parquet``. No fetch, no extraction,
no WKB decoding.

Features land with type ``global:establishment`` (the existing POI
type from r003). A future migration can add a more specific
``amenity_point`` type when the difference matters; for now the
extra precision isn't earning its keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from reimagina.data import catalog as cat
from reimagina.data import feature as feat

from . import silver
from .loader import load_vector_source


# ─── Constants ─────────────────────────────────────────────────────────────


# Default location relative to repo root; overridable for tests.
DEFAULT_SOURCE_ROOT = Path("data/sources/azure")

# Azure container path used as the lineage prefix in
# data_source_catalog.storage_uri.
AZURE_RAW_PREFIX = "azure://cfcetlsadls/raw"

# Use the existing global:establishment feature type from r003 for now.
# All amenity geoparquets land here regardless of provider.
POINTS_FEATURE_TYPE_CONTEXT = "global"
POINTS_FEATURE_TYPE_SLUG = "establishment"


# ─── Spec ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PointsLayerSpec:
    """One point-geometry amenity layer described end-to-end.

    Mirrors ``CDMXLayerSpec`` / ``InegiMarcoLayerSpec`` so stage + load
    are fully driven by the spec — no per-layer branching in the caller.
    """

    key: str
    geoparquet_relative: str
    """Path relative to ``source_root`` (default ``data/sources/azure/``)."""

    azure_subpath: str
    """Path under ``cfcetlsadls/raw/`` used to build the lineage URI."""

    display_name: str
    provider: str
    license: str
    version: str
    notes: str
    source_record_ref_col: str = "id"
    name_col: str | None = "category"
    """``category`` doubles as the human-readable label until a richer
    name column appears in upstream amenity feeds."""
    attributes_cols: tuple[str, ...] = (
        "category",
        "capacity",
        "fuente",
        "fecha",
        "entidad",
    )


TLALPAN_AMENITIES = PointsLayerSpec(
    key="tlalpan_amenities",
    geoparquet_relative="sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet",
    azure_subpath="sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet",
    display_name="CDMX · Tlalpan amenities (SIG-CDMX + DENUE)",
    provider="SIG-CDMX",
    license="Datos abiertos de la CDMX",
    version="2026-03",
    notes=(
        "Combined amenity dataset for CDMX-Tlalpan: SIG-CDMX official "
        "equipamientos plus a DENUE-derived layer, deduplicated upstream. "
        "1.8k point features."
    ),
)


MEXICALI_AMENITIES = PointsLayerSpec(
    key="mexicali_amenities",
    geoparquet_relative="exe/002_mexicali/equipamientos.geoparquet",
    azure_subpath="exe/002_mexicali/equipamientos.geoparquet",
    display_name="Mexicali · Amenities (IMIP / DENUE)",
    provider="IMIP Mexicali",
    license="Datos abiertos del IMIP",
    version="2025-05",
    notes=(
        "DENUE-derived amenities for Mexicali (Baja California). "
        "1.5k point features."
    ),
)


LAYER_SPECS: dict[str, PointsLayerSpec] = {
    spec.key: spec for spec in (TLALPAN_AMENITIES, MEXICALI_AMENITIES)
}

DEFAULT_LOAD_ORDER: tuple[str, ...] = ("tlalpan_amenities", "mexicali_amenities")


# ─── Source registration ───────────────────────────────────────────────────


def ensure_layer_source(conn, spec: PointsLayerSpec) -> str:
    """Idempotently register the data_source_catalog row for one amenity layer."""
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
        integration_role="enrichment",
        storage_uri=f"{AZURE_RAW_PREFIX}/{spec.azure_subpath}",
        version=spec.version,
        license=spec.license,
        notes=spec.notes,
    )


# ─── Stage + load ──────────────────────────────────────────────────────────


def stage_layer(
    spec: PointsLayerSpec,
    *,
    source_id: str,
    source_root: Path,
) -> Path:
    """Read the amenity GeoParquet, clean it, and write it to silver.

    Source data is already EPSG:4326 GeoParquet — ``silver.read_vector``
    handles ``.parquet`` natively, so the bronze landing step is
    skipped (the source itself is the canonical immutable artifact).
    """
    source_path = source_root / spec.geoparquet_relative
    if not source_path.exists():
        raise FileNotFoundError(
            f"Amenity GeoParquet not found: {source_path}. "
            "Verify the data lake mirror under data/sources/azure/ exists."
        )
    gdf = silver.read_vector(source_path)
    if gdf.empty:
        raise ValueError(
            f"Amenity layer {spec.key} resolved to zero rows. "
            "Check the source file has not been truncated."
        )
    return silver.save_silver_vector(gdf, source_id, f"{spec.key}_silver.parquet")


def load_layer(
    conn,
    spec: PointsLayerSpec,
    *,
    source_id: str,
    silver_path: Path,
    orchestrator_run_id: str,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Load a staged silver GeoParquet of points into curated_geo.feature."""
    type_id = feat.get_type_id(
        conn,
        POINTS_FEATURE_TYPE_CONTEXT,
        POINTS_FEATURE_TYPE_SLUG,
    )
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


# ─── End-to-end bootstrap ──────────────────────────────────────────────────


def bootstrap(
    conn,
    *,
    layers: list[str] | None = None,
    source_root: Path | None = None,
    orchestrator_run_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stage + load each requested amenity layer.

    ``source_root`` defaults to the repo-relative ``data/sources/azure``
    path; tests override it to point at fixtures.
    """
    requested = list(layers) if layers else list(DEFAULT_LOAD_ORDER)
    unknown = [k for k in requested if k not in LAYER_SPECS]
    if unknown:
        raise ValueError(f"Unknown amenity layer key(s): {unknown}")
    ordered = [k for k in DEFAULT_LOAD_ORDER if k in requested]

    if source_root is not None:
        root = Path(source_root)
    else:
        root = DEFAULT_SOURCE_ROOT
    if not root.is_absolute():
        # Anchor at repo root (this file lives at packages/py/reimagina/pipeline/points.py).
        root = Path(__file__).resolve().parents[4] / root

    run_id = orchestrator_run_id or str(uuid4())

    feat.seed_feature_types(conn)
    conn.commit()

    per_layer: list[dict[str, Any]] = []
    for key in ordered:
        spec = LAYER_SPECS[key]
        source_id = ensure_layer_source(conn, spec)
        conn.commit()

        silver_path = stage_layer(spec, source_id=source_id, source_root=root)

        load_result = load_layer(
            conn,
            spec,
            source_id=source_id,
            silver_path=silver_path,
            orchestrator_run_id=run_id,
            code_version=code_version,
        )
        per_layer.append(
            {
                "layer": key,
                "type_slug": POINTS_FEATURE_TYPE_SLUG,
                "source_id": source_id,
                "silver_path": str(silver_path),
                **load_result,
            }
        )

    return {
        "orchestrator_run_id": run_id,
        "layers": per_layer,
        "layers_loaded": [row["layer"] for row in per_layer],
    }
