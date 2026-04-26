"""Ingest INEGI Marco Geoestadístico 2024 (data lake) into curated_geo.feature.

Targets the data downloaded from the cfcetlsadls Azure data lake at
``inegi/marco_geostadistico/2024/``. Each layer (areas_estatales,
areas_municipales, agebs_urbanas, manzanas_urbanas) is a hive-partitioned
parquet directory with a WKB BLOB ``geometry`` column.

Design choices vs ``pipeline/cdmx.py``:

- No fetch step. The data lake mirror lives on local disk under
  ``data/sources/azure/`` (downloaded via Storage Explorer; auth was
  blocked at the tenant level). ``data_source_catalog.storage_uri``
  retains the original ``azure://`` URI for lineage.
- No bronze landing. The source is already a stable, immutable parquet
  directory; copying it into ``data/bronze/<source_id>/`` would just
  duplicate hundreds of MB. Silver is generated directly from the source.
- Geometry is decoded from WKB BLOB → shapely with ``shapely.wkb.loads``
  before being handed to ``silver.clean_vector``. Source CRS is EPSG:4326
  (verified by sampling — bounds are in lon/lat range).
- Always filter by ``CVE_ENT`` at scan time so the curated table doesn't
  balloon to all of Mexico (~tens of millions of blocks). The pilot AOIs
  cover CVE_ENT IN ('02','09'); other entidades are a future op.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import geopandas as gpd
import pyarrow as pa
import pyarrow.dataset as pads
import shapely.wkb

from reimagina.data import catalog as cat
from reimagina.data import feature as feat

from . import silver
from .loader import load_vector_source


INEGI_MARCO_PROVIDER = "INEGI"
INEGI_MARCO_LICENSE = "Libre uso — Términos de Uso de la Información del INEGI"
INEGI_MARCO_VERSION = "2024.01"

# Source data is in EPSG:4326 already (verified by sampling MultiPolygon
# bounds — they were lon/lat values, not projected metres).
INEGI_MARCO_SOURCE_CRS = "EPSG:4326"

# Default location relative to repo root; overridable for tests.
DEFAULT_SOURCE_ROOT = Path("data/sources/azure/inegi/marco_geostadistico/2024")

# Azure container path used for lineage in data_source_catalog.storage_uri.
INEGI_MARCO_AZURE_PREFIX = "azure://cfcetlsadls/raw/inegi/marco_geostadistico/2024"


@dataclass(frozen=True)
class InegiMarcoLayerSpec:
    """One INEGI marco layer described end-to-end.

    Mirrors ``CDMXLayerSpec`` from ``pipeline/cdmx.py`` so the
    stage→load shape is fully driven by data, not branching.
    """

    key: str
    parquet_subdir: str
    feature_type_slug: str
    source_record_ref_col: str
    name_col: str | None
    attributes_cols: tuple[str, ...]
    display_name: str


INEGI_MARCO_STATES = InegiMarcoLayerSpec(
    key="areas_estatales",
    parquet_subdir="areas_estatales.parquet",
    feature_type_slug="state",
    source_record_ref_col="CVEGEO",
    name_col="NOMGEO",
    attributes_cols=("CVE_ENT", "NOMGEO"),
    display_name="México · Estados (INEGI Marco Geoestadístico 2024)",
)

INEGI_MARCO_MUNICIPALITIES = InegiMarcoLayerSpec(
    key="areas_municipales",
    parquet_subdir="areas_municipales.parquet",
    feature_type_slug="municipality",
    source_record_ref_col="CVEGEO",
    name_col="NOMGEO",
    attributes_cols=("CVE_ENT", "CVE_MUN", "NOMGEO"),
    display_name="México · Municipios (INEGI Marco Geoestadístico 2024)",
)

INEGI_MARCO_AGEBS = InegiMarcoLayerSpec(
    key="agebs_urbanas",
    parquet_subdir="agebs_urbanas.parquet",
    feature_type_slug="ageb",
    source_record_ref_col="CVEGEO",
    name_col=None,
    attributes_cols=("CVE_ENT", "CVE_MUN", "CVE_LOC", "CVE_AGEB"),
    display_name="México · AGEBs urbanos (INEGI Marco Geoestadístico 2024)",
)

INEGI_MARCO_BLOCKS = InegiMarcoLayerSpec(
    key="manzanas_urbanas",
    parquet_subdir="manzanas_urbanas.parquet",
    feature_type_slug="block",
    source_record_ref_col="CVEGEO",
    name_col=None,
    attributes_cols=("CVE_ENT", "CVE_MUN", "CVE_LOC", "CVE_AGEB", "CVE_MZA", "AMBITO", "TIPOMZA"),
    display_name="México · Manzanas urbanas (INEGI Marco Geoestadístico 2024)",
)

LAYER_SPECS: dict[str, InegiMarcoLayerSpec] = {
    spec.key: spec for spec in (
        INEGI_MARCO_STATES,
        INEGI_MARCO_MUNICIPALITIES,
        INEGI_MARCO_AGEBS,
        INEGI_MARCO_BLOCKS,
    )
}

# Coarse → fine. Same intent as cdmx.py: parents available before children,
# so downstream AOI overlap and aggregation can lean on them.
DEFAULT_LOAD_ORDER: tuple[str, ...] = (
    "areas_estatales",
    "areas_municipales",
    "agebs_urbanas",
    "manzanas_urbanas",
)


# ─── Read + decode ─────────────────────────────────────────────────────────


def read_layer_to_geodataframe(
    parquet_dir: Path,
    *,
    cve_ent_filter: list[str] | None,
) -> gpd.GeoDataFrame:
    """Read a hive-partitioned INEGI marco parquet, decode WKB into shapely.

    Filters by ``CVE_ENT`` at scan time via the partition pruning in
    ``pyarrow.dataset`` so we never materialise rows for entidades outside
    the pilot scope.

    INEGI codes are **zero-padded strings** (``"09"`` is CDMX, not 9).
    Pyarrow's default hive-partitioning detection coerces partition values
    that look numeric to int — that would silently corrupt CVE_ENT for
    every entidad below 10. We pin the partition schema explicitly to
    ``string`` to keep the leading zeros intact.

    Returns an EPSG:4326 GeoDataFrame.
    """
    partitioning = pads.HivePartitioning(pa.schema([("CVE_ENT", pa.string())]))
    dataset = pads.dataset(str(parquet_dir), format="parquet", partitioning=partitioning)
    if cve_ent_filter:
        # Hive partition values arrive as strings from the directory names.
        # Coerce caller input to str so callers can pass either ['02','09']
        # or [2, 9] without surprises (the latter would otherwise miss
        # because "9" != "09" in the partition column).
        filter_values = [_normalise_cve_ent(v) for v in cve_ent_filter]
        flt = pads.field("CVE_ENT").isin(filter_values)
        table = dataset.to_table(filter=flt)
    else:
        table = dataset.to_table()
    df = table.to_pandas()
    if df.empty:
        return gpd.GeoDataFrame(
            df,
            geometry=gpd.GeoSeries([], crs=INEGI_MARCO_SOURCE_CRS),
        )
    df["geometry"] = df["geometry"].apply(shapely.wkb.loads)
    return gpd.GeoDataFrame(df, geometry="geometry", crs=INEGI_MARCO_SOURCE_CRS)


def _normalise_cve_ent(value: int | str) -> str:
    """Coerce a CVE_ENT input to the canonical 2-digit zero-padded form.

    ``2`` and ``"2"`` and ``"02"`` all become ``"02"``. The data lake's
    partition directories use the zero-padded string and that's what the
    in-memory column ends up as after the explicit HivePartitioning above,
    so the filter has to match the same shape.
    """
    text = str(value)
    return text.zfill(2) if text.isdigit() and len(text) <= 2 else text


# ─── Source registration ───────────────────────────────────────────────────


def ensure_layer_source(conn, spec: InegiMarcoLayerSpec) -> str:
    """Idempotently register the data_source_catalog row for one INEGI layer.

    ``storage_uri`` keeps the upstream Azure URL for lineage even though the
    pipeline reads the locally-mirrored copy.
    """
    existing = cat.get_source(
        conn,
        name=spec.display_name,
        provider=INEGI_MARCO_PROVIDER,
        version=INEGI_MARCO_VERSION,
    )
    if existing is not None:
        return str(existing["id"])
    return cat.register_source(
        conn,
        name=spec.display_name,
        provider=INEGI_MARCO_PROVIDER,
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri=f"{INEGI_MARCO_AZURE_PREFIX}/{spec.parquet_subdir}",
        version=INEGI_MARCO_VERSION,
        license=INEGI_MARCO_LICENSE,
        notes=(
            f"INEGI Marco Geoestadístico 2024 — {spec.feature_type_slug}. "
            "Read from the cfcetlsadls Azure data lake "
            "(downloaded locally via Storage Explorer for ingest)."
        ),
    )


# ─── Stage + load ──────────────────────────────────────────────────────────


def stage_layer(
    spec: InegiMarcoLayerSpec,
    *,
    source_id: str,
    source_root: Path,
    cve_ent_filter: list[str] | None,
) -> Path:
    """Read filtered partitioned parquet → silver GeoParquet under data/silver/<source_id>/."""
    parquet_dir = source_root / spec.parquet_subdir
    if not parquet_dir.exists():
        raise FileNotFoundError(
            f"INEGI marco source not found: {parquet_dir}. "
            "Verify the data lake mirror at data/sources/azure/inegi/marco_geostadistico/2024/ exists."
        )
    gdf = read_layer_to_geodataframe(parquet_dir, cve_ent_filter=cve_ent_filter)
    if gdf.empty:
        raise ValueError(
            f"No rows for layer {spec.key} with cve_ent_filter={cve_ent_filter}. "
            "Check the filter and that the source partitions exist."
        )
    cleaned = silver.clean_vector(gdf)
    return silver.save_silver_vector(cleaned, source_id, f"{spec.key}_silver.parquet")


def load_layer(
    conn,
    spec: InegiMarcoLayerSpec,
    *,
    source_id: str,
    silver_path: Path,
    orchestrator_run_id: str,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Load a staged silver GeoParquet into curated_geo.feature."""
    type_id = feat.get_type_id(conn, "MEX", spec.feature_type_slug)
    return load_vector_source(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        type_id=type_id,
        dataset_version=INEGI_MARCO_VERSION,
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
    cve_ent_filter: list[str] | None = None,
    source_root: Path | None = None,
    orchestrator_run_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Stage + load each requested layer for the given entidades.

    ``cve_ent_filter`` is the workhorse: keep it small (e.g. ``['02','09']``)
    or you'll ingest all of Mexico.

    ``source_root`` defaults to the repo-relative
    ``data/sources/azure/inegi/marco_geostadistico/2024`` path; tests can
    point it at a fixture directory.
    """
    requested = list(layers) if layers else list(DEFAULT_LOAD_ORDER)
    unknown = [k for k in requested if k not in LAYER_SPECS]
    if unknown:
        raise ValueError(f"Unknown INEGI marco layer key(s): {unknown}")
    ordered = [k for k in DEFAULT_LOAD_ORDER if k in requested]

    if source_root is not None:
        root = Path(source_root)
    else:
        root = DEFAULT_SOURCE_ROOT
    if not root.is_absolute():
        # Anchor at repo root (this file lives at packages/py/reimagina/pipeline/inegi_marco.py).
        root = Path(__file__).resolve().parents[4] / root

    run_id = orchestrator_run_id or str(uuid4())

    feat.seed_feature_types(conn)
    conn.commit()

    per_layer: list[dict[str, Any]] = []
    for key in ordered:
        spec = LAYER_SPECS[key]
        source_id = ensure_layer_source(conn, spec)
        conn.commit()

        silver_path = stage_layer(
            spec,
            source_id=source_id,
            source_root=root,
            cve_ent_filter=cve_ent_filter,
        )

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
            "silver_path": str(silver_path),
            "cve_ent_filter": cve_ent_filter,
            **load_result,
        })

    return {
        "orchestrator_run_id": run_id,
        "layers": per_layer,
        "layers_loaded": [row["layer"] for row in per_layer],
        "cve_ent_filter": cve_ent_filter,
    }
