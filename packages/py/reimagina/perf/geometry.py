"""Shared geometry probes for deterministic PostGIS benchmark inputs."""

from __future__ import annotations


def bbox_from_extent(
    extent: tuple[float, float, float, float],
    *,
    width_ratio: float,
    height_ratio: float,
) -> tuple[float, float, float, float]:
    """Return a centred, padded viewport within an extent."""
    min_x, min_y, max_x, max_y = extent
    width = max_x - min_x
    height = max_y - min_y
    center_x = min_x + width / 2
    center_y = min_y + height / 2
    half_width = max(width * width_ratio / 2, 0.0001)
    half_height = max(height * height_ratio / 2, 0.0001)
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def bbox_around_feature(conn) -> tuple[float, float, float, float]:
    """Return a small viewport guaranteed to contain one canonical feature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              ST_XMin(ST_Envelope(geometry)),
              ST_YMin(ST_Envelope(geometry)),
              ST_XMax(ST_Envelope(geometry)),
              ST_YMax(ST_Envelope(geometry))
            FROM curated_geo.feature
            ORDER BY id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None or any(value is None for value in row):
        raise ValueError("No canonical features are loaded. Benchmarks require feature data.")
    min_x, min_y, max_x, max_y = (float(value) for value in row)
    padding = 0.0001
    return (min_x - padding, min_y - padding, max_x + padding, max_y + padding)
