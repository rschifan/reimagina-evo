"""File asset registration for raster / imagery / point-cloud sources."""
from __future__ import annotations
from datetime import datetime

from reimagina.storage import backend_for_connection


def register_file_asset(
    conn,
    *,
    source_id: str,
    ingestion_run_id: str,
    storage_uri: str,
    dataset_version: str,
    format: str | None = None,
    crs: str | None = None,
    resolution_m: float | None = None,
    band_description: dict | None = None,
    captured_at: datetime | None = None,
) -> str:
    """Register a file asset in object storage metadata.

    The trigger on the table rejects sources whose storage_type is not
    raster, imagery, or point_cloud.

    Returns the new asset UUID.
    """
    return backend_for_connection(conn).file_asset_repo().register_file_asset(
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        storage_uri=storage_uri,
        dataset_version=dataset_version,
        format=format,
        crs=crs,
        resolution_m=resolution_m,
        band_description=band_description,
        captured_at=captured_at,
    )
