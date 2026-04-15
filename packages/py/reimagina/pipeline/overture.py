"""Overture Maps first-source helpers for Mexico City onboarding."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import serving
from reimagina.storage import backend_for_connection
from . import bronze, silver
from .loader import load_vector_source


MEXICO_CITY_BBOX = (-99.597, 18.992, -98.606, 19.921)
OVERTURE_PROVIDER = "Overture Maps Foundation"
OVERTURE_LICENSE = "ODbL-1.0 / Overture terms"
OVERTURE_BUILDINGS_SOURCE_NAME = "Overture Buildings - Mexico City BBox"
OVERTURE_BUILDINGS_TYPE = "building"
OVERTURE_BUILDINGS_THEME = "buildings"
OVERTURE_RELEASE_S3_ROOT = "s3://overturemaps-us-west-2/release"
OVERTURE_S3_REGION = "us-west-2"
OVERTURE_BUILDINGS_FILENAME = "overture_buildings_mexico_city.parquet"
OVERTURE_BUILDINGS_SILVER_FILENAME = "overture_buildings_mexico_city_silver.parquet"
OVERTURE_BUILDINGS_NOTES = (
    "First example source: Overture Mexico City buildings subset clipped to the "
    "broader Mexico City bounding box."
)
OVERTURE_BUILDINGS_ATTRIBUTES = [
    "subtype",
    "class",
    "height",
    "num_floors",
    "names",
    "sources",
]
OVERTURE_PENDING_STORAGE_URI = "pending://overture/buildings/mexico-city"


@dataclass(frozen=True)
class OvertureSourceSpec:
    name: str
    provider: str
    storage_type: str
    integration_role: str
    version: str
    filename: str
    bbox: tuple[float, float, float, float]
    feature_type_context: str
    feature_type_slug: str
    notes: str
    license: str | None = None


OVERTURE_BUILDINGS_MEXICO_CITY = OvertureSourceSpec(
    name=OVERTURE_BUILDINGS_SOURCE_NAME,
    provider=OVERTURE_PROVIDER,
    storage_type="vector",
    integration_role="canonical_entity",
    version="2026-02-18.0",
    filename=OVERTURE_BUILDINGS_FILENAME,
    bbox=MEXICO_CITY_BBOX,
    feature_type_context="global",
    feature_type_slug="building",
    notes=OVERTURE_BUILDINGS_NOTES,
    license=OVERTURE_LICENSE,
)
def overture_theme_type_glob(
    *,
    release: str = OVERTURE_BUILDINGS_MEXICO_CITY.version,
    theme: str = OVERTURE_BUILDINGS_THEME,
    feature_type: str = OVERTURE_BUILDINGS_TYPE,
) -> str:
    """Return the cloud GeoParquet glob for a pinned Overture release."""
    return (
        f"{OVERTURE_RELEASE_S3_ROOT}/{release}/"
        f"theme={theme}/type={feature_type}/*.parquet"
    )


def overture_buildings_extract_sql(
    *,
    output_path: str | Path = OVERTURE_BUILDINGS_FILENAME,
    source_glob: str | None = None,
    bbox: tuple[float, float, float, float] = MEXICO_CITY_BBOX,
) -> str:
    """Return the DuckDB SQL used to create a local GeoParquet subset.

    The local output becomes the canonical Bronze artifact for this first source.
    Remote Overture sources use bbox metadata columns for fast cloud-native
    filtering; local GeoParquet sources fall back to geometry intersection.
    """
    source_glob = source_glob or overture_theme_type_glob()
    min_lon, min_lat, max_lon, max_lat = bbox
    if str(source_glob).startswith(("s3://", "https://")):
        where_clause = f"""
    WHERE
        bbox.xmax >= {min_lon} AND bbox.xmin <= {max_lon} AND
        bbox.ymax >= {min_lat} AND bbox.ymin <= {max_lat}
""".rstrip()
    else:
        where_clause = f"""
    WHERE ST_Intersects(
        geometry,
        ST_MakeEnvelope({min_lon}, {min_lat}, {max_lon}, {max_lat})
    )
""".rstrip()
    return f"""
COPY (
    SELECT *
    FROM read_parquet('{source_glob}')
{where_clause}
) TO '{Path(output_path)}' (FORMAT PARQUET);
""".strip()


def extract_overture_buildings_subset(
    *,
    output_path: str | Path = OVERTURE_BUILDINGS_FILENAME,
    source_glob: str | None = None,
    bbox: tuple[float, float, float, float] = MEXICO_CITY_BBOX,
) -> Path:
    """Extract the Mexico City buildings subset from Overture GeoParquet using DuckDB."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        if (source_glob or overture_theme_type_glob()).startswith("s3://"):
            conn.execute(f"SET s3_region='{OVERTURE_S3_REGION}';")
        conn.execute(
            overture_buildings_extract_sql(
                output_path=output_path,
                source_glob=source_glob,
                bbox=bbox,
            )
        )
    finally:
        conn.close()
    return output_path


def register_overture_buildings_source(
    conn,
    *,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
    version: str = OVERTURE_BUILDINGS_MEXICO_CITY.version,
) -> str:
    """Register the canonical first-source Overture Buildings Mexico City subset."""
    return cat.register_source(
        conn,
        name=OVERTURE_BUILDINGS_MEXICO_CITY.name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        storage_type=OVERTURE_BUILDINGS_MEXICO_CITY.storage_type,
        integration_role=OVERTURE_BUILDINGS_MEXICO_CITY.integration_role,
        storage_uri=storage_uri,
        version=version,
        license=OVERTURE_BUILDINGS_MEXICO_CITY.license,
        notes=OVERTURE_BUILDINGS_MEXICO_CITY.notes,
    )


def ensure_overture_buildings_source(
    conn,
    *,
    version: str = OVERTURE_BUILDINGS_MEXICO_CITY.version,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
) -> str:
    """Idempotently ensure the canonical first Overture source exists."""
    existing = cat.get_source(
        conn,
        name=OVERTURE_BUILDINGS_MEXICO_CITY.name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        version=version,
    )
    if existing is not None:
        if storage_uri and existing["storage_uri"] != storage_uri:
            cat.update_source_storage_uri(conn, str(existing["id"]), storage_uri)
        return str(existing["id"])
    return register_overture_buildings_source(conn, storage_uri=storage_uri, version=version)


def stage_overture_buildings_subset(
    raw_path: str | Path,
    *,
    source_id: str,
    bronze_filename: str = OVERTURE_BUILDINGS_FILENAME,
    silver_filename: str = OVERTURE_BUILDINGS_SILVER_FILENAME,
) -> tuple[Path, Path]:
    """Land a raw GeoParquet subset to Bronze and write a cleaned Silver GeoParquet."""
    bronze_path = bronze.land_file(raw_path, source_id)
    gdf = silver.read_vector(bronze_path)
    silver_path = silver.save_silver_vector(gdf, source_id, silver_filename)
    return bronze_path, silver_path


def bootstrap_overture_buildings_subset(
    conn,
    *,
    raw_path: str | Path,
    orchestrator_run_id: str,
    code_version: str = "dev",
    dataset_version: str = OVERTURE_BUILDINGS_MEXICO_CITY.version,
) -> dict[str, Any]:
    """Run the first-source flow: register, Bronze/Silver stage, then curated load."""
    feat.seed_feature_types(conn)
    conn.commit()

    source_id = ensure_overture_buildings_source(conn, version=dataset_version)
    conn.commit()

    bronze_path, silver_path = stage_overture_buildings_subset(raw_path, source_id=source_id)
    cat.update_source_storage_uri(conn, source_id, bronze.bronze_uri(source_id, bronze_path.name))
    conn.commit()

    load_result = load_overture_buildings_subset(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        dataset_version=dataset_version,
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
    )
    return {
        "source_id": source_id,
        "bronze_path": str(bronze_path),
        "silver_path": str(silver_path),
        **load_result,
    }


def load_overture_buildings_subset(
    conn,
    *,
    source_id: str,
    silver_path: str | Path,
    dataset_version: str = OVERTURE_BUILDINGS_MEXICO_CITY.version,
    orchestrator_run_id: str,
    code_version: str = "dev",
) -> dict[str, str | int]:
    """Load a cleaned Overture buildings subset into curated_geo.feature."""
    type_id = feat.get_type_id(
        conn,
        OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_context,
        OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug,
    )
    return load_vector_source(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        type_id=type_id,
        dataset_version=dataset_version,
        source_record_ref_col="id",
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
        name_col=None,
        attributes_cols=OVERTURE_BUILDINGS_ATTRIBUTES,
    )


def validate_overture_buildings_layer(conn) -> dict[str, Any]:
    """Return a compact validation snapshot for the first spatial layer."""
    backend = backend_for_connection(conn)
    type_slug = OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug
    count_sql = """
        SELECT COUNT(*)
        FROM serving_geo.feature_v
        WHERE type_slug = ?
    """
    sample_sql = """
        SELECT
            source_id,
            source_record_ref,
            ST_GeometryType(geometry) AS geometry_type,
            attributes
        FROM serving_geo.feature_v
        WHERE type_slug = ?
        ORDER BY created_at
        LIMIT 3
    """
    if backend.name == "postgres":
        count_sql = count_sql.replace("?", "%s")
        sample_sql = sample_sql.replace("?", "%s")
        with conn.cursor() as cur:
            cur.execute(count_sql, (type_slug,))
            count = int(cur.fetchone()[0])
            cur.execute(sample_sql, (type_slug,))
            samples = [
                {
                    "source_id": str(row[0]),
                    "source_record_ref": row[1],
                    "geometry_type": row[2],
                    "attributes": row[3],
                }
                for row in cur.fetchall()
            ]
    else:
        count = int(conn.execute(count_sql, [type_slug]).fetchone()[0])
        samples = [
            {
                "source_id": str(row[0]),
                "source_record_ref": row[1],
                "geometry_type": row[2],
                "attributes": row[3],
            }
            for row in conn.execute(sample_sql, [type_slug]).fetchall()
        ]

    return {
        "count": count,
        "sample_features": samples,
        "queryable_through_serving": count >= 0,
        "type_slug": type_slug,
    }


def sample_serving_buildings(
    conn,
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return a small serving-layer sample for building features."""
    return serving.get_features(conn, type_slug=OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug)[:limit]
