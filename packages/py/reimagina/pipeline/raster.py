"""Raster ingest pipeline — bronze land + silver COG conversion + register.

One entry point: :func:`ingest_raster_source`. The design doc calls
this R2 in the raster-integration playbook.

Scope locked by the design doc (Rev 7):

- Single-band GDAL-openable input only. HDF / multi-band NetCDF
  containers are out of v1 scope; callers pre-extract with external
  tooling (``gdal_translate``, ``gdalwarp``). A raster with >1 band
  raises :class:`UnsupportedRasterError` at ingest time.
- Silver COG stays in the input's **native CRS** — no on-ingest
  reprojection of pixel values. The zonal-stats worker reprojects
  feature geometries to the raster CRS instead (rasterstats default).
- ``resolution_m`` is populated only when the native CRS is projected
  (units=metre) AND the transform is north-up with square pixels;
  otherwise stored as NULL.
- ``asset_extent_geometry`` is intentionally not populated in v1.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import rasterio
from rasterio.shutil import copy as rio_copy

from reimagina.data import catalog as cat
from reimagina.storage import backend_for_connection

from . import bronze
from .silver import SILVER_DIR


logger = logging.getLogger(__name__)


class UnsupportedRasterError(ValueError):
    """Raised when the input raster is outside v1 scope (multi-band,
    unopenable, or any other condition flagged by the pipeline)."""


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a successful :func:`ingest_raster_source` call."""

    source_id: str
    asset_id: str
    ingestion_run_id: str
    layer_id: str
    silver_path: Path
    storage_uri: str
    crs: str
    resolution_m: float | None


def ingest_raster_source(
    conn,
    *,
    raw_path: str | Path,
    source_name: str,
    provider: str,
    version: str,
    layer_slug: str,
    aggregation_kind: str = "intensive",
    logical_name: str | None = None,
    description: str | None = None,
    colormap_name: str | None = None,
    rescale: str | None = None,
    license: str | None = None,
    captured_at: datetime | None = None,
    code_version: str = "dev",
    orchestrator: str = "cli",
    orchestrator_run_id: str | None = None,
    minzoom: int = 5,
    maxzoom: int = 14,
    published: bool = True,
) -> IngestResult:
    """Register → stage → convert → publish a single-band raster source.

    Steps (all inside one ingestion_run for provenance):
      1. Register or upsert ``data_source_catalog`` row
         (``storage_type='raster'``, ``integration_role='enrichment'``).
      2. Start an ``ingestion_run`` row (``transformation_step='raster_landing'``).
      3. Land raw file to ``data/bronze/<source_id>/``.
      4. Re-encode to COG under ``data/silver/<source_id>/`` in the
         input's native CRS. Raises :class:`UnsupportedRasterError` if
         the input has >1 band.
      5. Extract metadata (CRS, resolution_m, band_description).
      6. Call :meth:`FileAssetRepository.register_file_asset`.
      7. Call :meth:`RasterLayerRepository.register_layer`.
      8. Complete the ingestion_run with status='succeeded' + details.
         On any failure, complete with status='failed' and re-raise.

    Returns an :class:`IngestResult` describing the persisted state.
    """
    raw_path = Path(raw_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"Raw raster not found: {raw_path}")
    if aggregation_kind not in ("intensive", "extensive"):
        raise ValueError(
            f"aggregation_kind must be 'intensive' or 'extensive', got {aggregation_kind!r}"
        )

    # File-level validation comes BEFORE any DB work so bad inputs
    # don't leave orphan source / ingestion_run rows behind. Opens the
    # raster once, checks band count, captures metadata we'll need
    # later. The raster is closed before the COG re-encode below.
    with rasterio.open(raw_path) as src:
        if src.count != 1:
            raise UnsupportedRasterError(
                f"Input raster has {src.count} bands; v1 supports single-band only. "
                f"Pre-extract with gdal_translate -b <N> and re-run."
            )
        native_crs = src.crs.to_string() if src.crs else None
        band_name = (src.descriptions[0] if src.descriptions else None) or "band_1"
        resolution_m = _resolution_in_metres(src)

    backend = backend_for_connection(conn)

    # Step 1 — register or upsert the source. The canonical storage_uri
    # will be the silver COG path; we set a placeholder here and
    # overwrite below once the silver artifact is on disk.
    source_id = cat.register_source(
        conn,
        name=source_name,
        provider=provider,
        storage_type="raster",
        integration_role="enrichment",
        storage_uri=f"file://data/silver/<pending>/{raw_path.name}",
        version=version,
        license=license,
        notes=description,
    )

    # Step 2 — start ingestion_run AND commit it immediately, so a later
    # SQL failure (e.g. UniqueViolation on layer_slug) can't roll back
    # the very row meant to record that failure. Every other loader
    # follows this pattern.
    ingestion_run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator=orchestrator,
        orchestrator_run_id=orchestrator_run_id or str(uuid4()),
        transformation_step="raster_landing",
        code_version=code_version,
    )
    conn.commit()

    try:
        # Step 3 — bronze. Immutable copy.
        bronze_path = bronze.land_file(raw_path, source_id)

        # Step 4 — silver. Re-encode to COG in native CRS. Metadata
        # (band count, native_crs, resolution_m) was captured before the
        # DB transaction started. Internal overviews come for free via
        # the COG driver.
        silver_dir = SILVER_DIR / source_id
        silver_dir.mkdir(parents=True, exist_ok=True)
        # Emit `<input-stem>.cog.tif` so downstream consumers can tell
        # at a glance which Silver files are COGs.
        silver_path = silver_dir / f"{raw_path.stem}.cog.tif"
        rio_copy(str(bronze_path), str(silver_path), driver="COG")

        # Step 5 — metadata for the registry tables. storage_uri uses
        # the reimagina-internal form 'file://data/<subpath>' — matches
        # the canonical URI parsed by reimagina.tiles.defaults.
        subpath = silver_path.relative_to(_data_root())
        storage_uri = f"file://data/{subpath.as_posix()}"

        # Overwrite the '<pending>' placeholder set in Step 1 now that the
        # real silver path is known. Keeps ``data_source_catalog.storage_uri``
        # aligned with ``file_asset.storage_uri`` — the convention every
        # other pipeline (cdmx, overture) already follows, and the one
        # ``explore.source_runs_v`` surfaces to operators.
        cat.update_source_storage_uri(conn, source_id, storage_uri)

        # Step 6 — file_asset.
        asset_id = backend.file_asset_repo().register_file_asset(
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            storage_uri=storage_uri,
            dataset_version=version,
            format="COG",
            crs=native_crs,
            resolution_m=resolution_m,
            band_description={"1": band_name},
            captured_at=captured_at,
        )

        # Step 7 — raster_layer registry row.
        layer_id = backend.raster_layer_repo().register_layer(
            source_id=source_id,
            asset_id=asset_id,
            layer_slug=layer_slug,
            logical_name=logical_name,
            description=description,
            colormap_name=colormap_name,
            rescale=rescale,
            minzoom=minzoom,
            maxzoom=maxzoom,
            published=published,
            aggregation_kind=aggregation_kind,
        )

        # Step 8 — success. Commit the registry writes + the succeeded
        # status together so partial state can't leak if the caller's
        # context manager later rolls back.
        cat.complete_ingestion_run(
            conn,
            ingestion_run_id,
            status="succeeded",
            details={
                "silver_path": str(silver_path),
                "storage_uri": storage_uri,
                "crs": native_crs,
                "resolution_m": resolution_m,
                "layer_slug": layer_slug,
            },
        )
        conn.commit()
    except Exception as exc:
        # Clear any aborted-transaction state before trying to write
        # the failure status: on Postgres a prior error (e.g.
        # UniqueViolation on layer_slug) leaves the connection in an
        # aborted state where every new statement fails until rollback.
        # Then re-try complete_ingestion_run in a fresh implicit txn.
        # Both rollback() and the finalise call are wrapped so the
        # original exception always propagates unmasked.
        try:
            conn.rollback()
        except Exception as rollback_exc:
            logger.warning(
                "failed to rollback aborted transaction before finalising "
                "raster ingest run %s: %s",
                ingestion_run_id, rollback_exc,
            )
        try:
            cat.complete_ingestion_run(
                conn,
                ingestion_run_id,
                status="failed",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            conn.commit()
        except Exception as finalise_exc:
            logger.warning(
                "failed to mark raster ingest run %s as failed; "
                "row may remain in running state. Finalise error: %s",
                ingestion_run_id, finalise_exc,
            )
        raise

    return IngestResult(
        source_id=source_id,
        asset_id=asset_id,
        ingestion_run_id=ingestion_run_id,
        layer_id=layer_id,
        silver_path=silver_path,
        storage_uri=storage_uri,
        crs=native_crs,
        resolution_m=resolution_m,
    )


def _data_root() -> Path:
    """Absolute data root — matches bronze.BRONZE_DIR / silver.SILVER_DIR."""
    # Use SILVER_DIR.parent instead of recomputing to keep a single
    # source of truth with pipeline/silver.py + pipeline/bronze.py.
    return SILVER_DIR.parent


def _resolution_in_metres(dataset) -> float | None:
    """Return ``abs(transform.a)`` when the raster's native CRS is
    projected in metres AND the transform is north-up with square
    pixels. Return ``None`` otherwise.

    Matches the design doc's explicit derivation rule (Rev 6) — we
    prefer NULL over inventing a misleading scalar for angular CRSes
    or non-square transforms.
    """
    if dataset.crs is None or not dataset.crs.is_projected:
        return None
    # Linear units: rasterio exposes them via crs.linear_units ('metre',
    # 'meter', 'foot', …) — accept any metre spelling.
    units = (dataset.crs.linear_units or "").lower()
    if units not in ("metre", "meter"):
        return None
    t = dataset.transform
    # t.b and t.d being 0 means axes-aligned (no rotation / shear).
    if not (math.isclose(t.b, 0.0, abs_tol=1e-9) and math.isclose(t.d, 0.0, abs_tol=1e-9)):
        return None
    # Square pixels: |a| == |e|.
    if not math.isclose(abs(t.a), abs(t.e), rel_tol=1e-6):
        return None
    return abs(t.a)


# Expose the data-root resolver so tests and downstream callers can
# query it without importing from a private module. Used in one-off
# scripts and test fixtures that want to inspect staged Silver files.
__all__ = [
    "IngestResult",
    "UnsupportedRasterError",
    "ingest_raster_source",
]
