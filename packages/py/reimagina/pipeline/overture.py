"""Overture Maps first-source helpers for Mexico City onboarding."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as pads
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
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
# Default Overture release for ingest.
#
# ⚠ **Overture keeps only the most recent release on S3.** Older ones are
# deleted, so any pin here has a shelf life measured in weeks and *will* stop
# resolving — this is the second time it has: 2026-02-18.0 was never published,
# and 2026-04-15.0 was published and then rotated away, after which every
# extract failed with a bare `FileNotFoundError` naming a path.
#
# Kept as a pin anyway, because the value is recorded as `dataset_version` on
# the source and on every row's provenance, and a moving default would make two
# ingests of "the same" data incomparable. The cost is paid at fetch time
# instead: `available_overture_releases()` turns the failure into a message that
# names what S3 actually has, so bumping this is a ten-second fix rather than an
# investigation. Override per-run with REIMAGINA_OVERTURE_RELEASE.
OVERTURE_DEFAULT_RELEASE = os.environ.get("REIMAGINA_OVERTURE_RELEASE", "2026-07-22.0")
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
    version=OVERTURE_DEFAULT_RELEASE,
    filename=OVERTURE_BUILDINGS_FILENAME,
    bbox=MEXICO_CITY_BBOX,
    feature_type_context="global",
    feature_type_slug="building",
    notes=OVERTURE_BUILDINGS_NOTES,
    license=OVERTURE_LICENSE,
)


def overture_theme_type_glob(
    *,
    release: str = OVERTURE_DEFAULT_RELEASE,
    theme: str = OVERTURE_BUILDINGS_THEME,
    feature_type: str = OVERTURE_BUILDINGS_TYPE,
) -> str:
    """Return the cloud GeoParquet glob for a pinned Overture release."""
    return (
        f"{OVERTURE_RELEASE_S3_ROOT}/{release}/"
        f"theme={theme}/type={feature_type}/*.parquet"
    )


def _s3_prefix_from_glob(source_glob: str) -> str:
    """Strip the ``s3://`` scheme and trailing ``/*.parquet`` glob so a
    pyarrow dataset / file selector can walk the release directory."""
    prefix = source_glob.split("://", 1)[-1]
    if prefix.endswith("/*.parquet"):
        prefix = prefix[: -len("/*.parquet")]
    return prefix


def available_overture_releases() -> list[str]:
    """Release folders currently on Overture's S3, oldest first.

    Overture deletes superseded releases, so this is the authoritative answer to
    "what can I actually pin to today" — and usually returns a single entry.
    """
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    root = OVERTURE_RELEASE_S3_ROOT.removeprefix("s3://")
    selector = pafs.FileSelector(root, recursive=False)
    return sorted(info.base_name for info in s3.get_file_info(selector))


def _extract_remote_overture_subset(
    source_glob: str,
    output_path: Path,
    bbox: tuple[float, float, float, float],
) -> None:
    """Read the pinned Overture S3 GeoParquet release and write the bbox
    subset locally. Filters on the GeoParquet ``bbox`` struct columns so
    pyarrow prunes non-overlapping row groups from row-group statistics
    without fetching them."""
    min_lon, min_lat, max_lon, max_lat = bbox
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    try:
        dataset = pads.dataset(
            _s3_prefix_from_glob(source_glob), filesystem=s3, format="parquet"
        )
    except FileNotFoundError as exc:
        # The pinned release has almost certainly been rotated off S3. Say so,
        # and say what is there — the bare error names a path and leaves the
        # reader to guess whether it is a typo, a permissions problem, or this.
        raise FileNotFoundError(
            f"Overture release not found: {exc}. Overture keeps only its most "
            f"recent release, so a pin older than a few weeks stops resolving. "
            f"Available now: {', '.join(available_overture_releases()) or '(none)'}. "
            f"Set REIMAGINA_OVERTURE_RELEASE, or bump OVERTURE_DEFAULT_RELEASE "
            f"in pipeline/overture.py."
        ) from exc
    flt = (
        (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "ymax") >= min_lat)
        & (pc.field("bbox", "ymin") <= max_lat)
    )
    # ponytail: materialises the whole metro subset in memory — fine for a
    # city bbox; switch to dataset.to_batches() + ParquetWriter if a larger
    # AOI OOMs.
    pq.write_table(dataset.to_table(filter=flt), str(output_path))


def _extract_local_overture_subset(
    source_glob: str,
    output_path: Path,
    bbox: tuple[float, float, float, float],
) -> None:
    """Filter a local GeoParquet to the bbox via geopandas ``.cx`` — a
    bounding-box spatial slice (geometry intersection with the bbox
    envelope)."""
    import geopandas as gpd

    min_lon, min_lat, max_lon, max_lat = bbox
    gdf = gpd.read_parquet(source_glob)
    gdf.cx[min_lon:max_lon, min_lat:max_lat].to_parquet(output_path)


def extract_overture_buildings_subset(
    *,
    output_path: str | Path = OVERTURE_BUILDINGS_FILENAME,
    source_glob: str | None = None,
    bbox: tuple[float, float, float, float] = MEXICO_CITY_BBOX,
    release: str = OVERTURE_DEFAULT_RELEASE,
) -> Path:
    """Extract an Overture buildings subset to local GeoParquet.

    Remote Overture releases (``s3://…``) are read with pyarrow over an
    anonymous S3 filesystem and filtered on the GeoParquet ``bbox`` struct
    columns for cloud-native predicate pushdown. A local GeoParquet
    ``source_glob`` falls back to a geometry bbox intersection via
    geopandas. The local output becomes the canonical Bronze artifact.

    Defaults match the legacy CDMX bbox. Override ``bbox`` for any other
    AOI (e.g. Mexicali). ``release`` selects the Overture S3 release
    folder; the default tracks the most recently verified snapshot
    (see :data:`OVERTURE_DEFAULT_RELEASE`).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    effective_glob = source_glob or overture_theme_type_glob(release=release)
    if str(effective_glob).startswith("s3://"):
        _extract_remote_overture_subset(effective_glob, output_path, bbox)
    else:
        _extract_local_overture_subset(effective_glob, output_path, bbox)
    return output_path


def register_overture_buildings_source(
    conn,
    *,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
    version: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
) -> str:
    """Register an Overture Buildings source row. ``source_name`` lets a
    caller register one row per AOI (e.g. one for CDMX, one for Mexicali)
    rather than collapsing every extraction into the same catalog row."""
    return cat.register_source(
        conn,
        name=source_name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        storage_type=OVERTURE_BUILDINGS_MEXICO_CITY.storage_type,
        integration_role=OVERTURE_BUILDINGS_MEXICO_CITY.integration_role,
        storage_uri=storage_uri,
        version=version,
        license=OVERTURE_BUILDINGS_MEXICO_CITY.license,
        notes=notes,
    )


def ensure_overture_buildings_source(
    conn,
    *,
    version: str = OVERTURE_DEFAULT_RELEASE,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
) -> str:
    """Idempotently ensure an Overture buildings source row exists. Keyed
    on (``source_name``, provider, ``version``) — distinct names produce
    distinct catalog rows."""
    existing = cat.get_source(
        conn,
        name=source_name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        version=version,
    )
    if existing is not None:
        if storage_uri and existing["storage_uri"] != storage_uri:
            cat.update_source_storage_uri(conn, str(existing["id"]), storage_uri)
        return str(existing["id"])
    return register_overture_buildings_source(
        conn,
        storage_uri=storage_uri,
        version=version,
        source_name=source_name,
        notes=notes,
    )


def stage_overture_buildings_subset(
    raw_path: str | Path,
    *,
    source_id: str,
    silver_filename: str = OVERTURE_BUILDINGS_SILVER_FILENAME,
) -> tuple[Path, Path]:
    """Land a raw GeoParquet subset to Bronze and write a cleaned Silver GeoParquet.

    There is no ``bronze_filename``: ``bronze.land_file`` keeps the raw file's
    own name, because a Bronze file is a verbatim immutable copy and renaming it
    would cost the only link back to what arrived. This signature used to accept
    one and ignore it, so a caller that passed it got a silently different path
    than it asked for. Callers that need the landed name read it off the
    returned path.
    """
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
    dataset_version: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
    aoi_id: str | None = None,
) -> dict[str, Any]:
    """Run the full ingest flow: register, Bronze/Silver stage, curated
    load, and (optionally) clip the just-loaded buildings to an AOI
    polygon so bbox-but-not-polygon orphans never persist."""
    feat.seed_feature_types(conn)
    conn.commit()

    # Lineage: the release this subset was cut from, which is where the data
    # came from. It used to be a `pending://` placeholder overwritten with the
    # local bronze path once landed — recording our copy and losing the
    # source. The bronze copy needs no column: it lives at
    # `data/bronze/{source_id}/` by construction. Passing it here also moves
    # a row registered under the old convention onto this one on re-ingest,
    # since `ensure_…` updates a URI that differs.
    source_id = ensure_overture_buildings_source(
        conn,
        version=dataset_version,
        source_name=source_name,
        notes=notes,
        storage_uri=overture_theme_type_glob(release=dataset_version),
    )
    conn.commit()

    bronze_path, silver_path = stage_overture_buildings_subset(
        raw_path, source_id=source_id
    )

    load_result = load_overture_buildings_subset(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        dataset_version=dataset_version,
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
    )

    clip_result: dict[str, int | str] = {}
    if aoi_id is not None:
        clip_result = _clip_buildings_to_aoi(
            conn,
            ingestion_run_id=str(load_result["ingestion_run_id"]),
            aoi_id=aoi_id,
        )
        conn.commit()

    return {
        "source_id": source_id,
        "bronze_path": str(bronze_path),
        "silver_path": str(silver_path),
        **load_result,
        **clip_result,
    }


def load_overture_buildings_subset(
    conn,
    *,
    source_id: str,
    silver_path: str | Path,
    dataset_version: str = OVERTURE_DEFAULT_RELEASE,
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


def _clip_buildings_to_aoi(
    conn, *, ingestion_run_id: str, aoi_id: str
) -> dict[str, int | str]:
    """Drop rows from this ingestion run whose centroid falls outside the
    AOI polygon. Bbox-based S3 extraction always returns a superset of
    the AOI; this guarantees ``aoi_feature`` recompute won't surface
    bbox-only orphans later."""
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM curated_geo.feature f
        USING curated_geo.area_of_interest aoi
        WHERE f.ingestion_run_id = %s
          AND aoi.id = %s
          AND NOT ST_Intersects(f.geometry, aoi.geometry)
        """,
        (ingestion_run_id, aoi_id),
    )
    return {"clipped_outside_aoi": cur.rowcount, "clipped_to_aoi_id": aoi_id}


def validate_overture_buildings_layer(conn) -> dict[str, Any]:
    """Return a compact validation snapshot for the first spatial layer."""
    reader = backend_for_connection(conn).serving_reader()
    type_slug = OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug
    count = reader.count_features(type_slug=type_slug)
    page = reader.get_features(type_slug=type_slug, limit=3)
    samples = [
        {
            "source_record_ref": row.get("name"),
            "attributes": row.get("attributes"),
        }
        for row in page["features"]
    ]
    return {
        "count": count,
        "sample_features": samples,
        "queryable_through_serving": count >= 0,
        "type_slug": type_slug,
    }
