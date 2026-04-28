"""Area of interest management and aoi_feature computation."""

from __future__ import annotations

from reimagina.storage import backend_for_connection


def create_aoi(
    conn,
    *,
    project_id: str,
    name: str,
    geometry_wkt: str,
    description: str | None = None,
) -> str:
    """Create a project area of interest. Returns the new AOI UUID."""
    return (
        backend_for_connection(conn)
        .aoi_repo()
        .create_aoi(
            project_id=project_id,
            name=name,
            geometry_wkt=geometry_wkt,
            description=description,
        )
    )


def compute_aoi_features(conn, aoi_id: str) -> int:
    """(Re)populate aoi_feature for a given AOI by intersecting with all canonical features.

    Deletes existing rows for the AOI first so the operation is idempotent.
    Measurements use the geography type for metric units (m² / m).

    Returns the total count of feature rows inserted.
    """
    return backend_for_connection(conn).aoi_repo().compute_overlaps(aoi_id)
