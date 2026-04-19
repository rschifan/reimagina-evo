"""Building density: pure math + compute worker.

Counts Overture buildings (MEX:building features) whose centroid falls inside
each MEX:block, divides by block area in hectares, and writes one per-block
`indicator_result` row. Blocks with no buildings get an explicit
`value_numeric = 0.0` row (quality_flag='measured') — a block that has simply
not been observed is semantically different from one we have confirmed is
empty, and zero-building blocks are real signal in urban form analysis.

Centroid-in-polygon is the counting rule: a building is attributed to exactly
one block, avoiding the double-counting that a geometric-intersection rule
would produce at block boundaries. Standard convention in urban analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reimagina.data.feature import get_type_id
from reimagina.indicators.catalog import URBAN_FORM_BLOCK_BUILDING_DENSITY
from reimagina.storage import backend_for_connection


_M2_PER_HECTARE = 10_000.0


def count_to_density(*, count: int, area_m2: float) -> float:
    """Buildings per hectare. Returns 0.0 for degenerate areas."""
    if area_m2 <= 0 or count < 0:
        return 0.0
    return count / (area_m2 / _M2_PER_HECTARE)


@dataclass(frozen=True)
class ComputeResult:
    """Outcome of a compute_block_building_density run."""

    job_run_id: str
    indicator_slug: str
    blocks_considered: int
    buildings_attributed: int
    rows_written: int
    aoi_id: str | None


def compute_block_building_density(
    conn,
    *,
    aoi_id: str | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Compute buildings-per-hectare for every MEX:block feature.

    Scope the run with `aoi_id` (the AOI's geometry filters *blocks*; every
    building whose centroid falls in an in-scope block is counted regardless
    of whether the building's polygon also touches the AOI). Writes rows
    through `bulk_write_numeric_results` so the block-level uniqueness and
    validation rules fire the same way on both backends.
    """
    backend = backend_for_connection(conn)
    spec = URBAN_FORM_BLOCK_BUILDING_DENSITY
    catalog = backend.indicator_catalog_repo().get_indicator(spec.slug)
    if catalog is None:
        raise RuntimeError(
            f"Indicator {spec.slug!r} not found in indicator_catalog. "
            "Run `indicators seed-catalog` first."
        )
    block_type_id = get_type_id(conn, "MEX", "block")
    building_type_id = get_type_id(conn, "global", "building")

    job_run_repo = backend.job_run_repo()
    job_run_id = job_run_repo.start_job(
        model_family=spec.model_family or "urban_form",
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
    )
    try:
        rows = _fetch_block_building_counts(
            backend=backend,
            block_type_id=block_type_id,
            building_type_id=building_type_id,
            aoi_id=aoi_id,
        )
        numeric_rows = [
            (feature_id, count_to_density(count=count, area_m2=area))
            for feature_id, count, area in rows
        ]
        result_repo = backend.indicator_result_repo()
        rows_written = result_repo.bulk_write_numeric_results(
            numeric_rows,
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )
        job_run_repo.complete_job(job_run_id, status="succeeded")
    except Exception as exc:
        job_run_repo.complete_job(
            job_run_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        blocks_considered=len(rows),
        buildings_attributed=sum(count for _, count, _ in rows),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )


# ─── Backend-specific counts readers ───────────────────────────────────────


def _fetch_block_building_counts(
    *,
    backend: Any,
    block_type_id: str,
    building_type_id: str,
    aoi_id: str | None,
) -> list[tuple[str, int, float]]:
    """Return (block_feature_id, building_count, area_m2) for each eligible block.

    Every in-scope block appears exactly once even when zero buildings match —
    the join is LEFT on the building side so the downstream compute worker
    can emit a measured 0.0/ha row rather than a missing one.
    """
    conn = backend.connection
    if backend.name == "postgres":
        return _fetch_block_building_counts_postgres(
            conn,
            block_type_id=block_type_id,
            building_type_id=building_type_id,
            aoi_id=aoi_id,
        )
    return _fetch_block_building_counts_duckdb(
        conn,
        block_type_id=block_type_id,
        building_type_id=building_type_id,
        aoi_id=aoi_id,
    )


def _fetch_block_building_counts_postgres(
    conn,
    *,
    block_type_id: str,
    building_type_id: str,
    aoi_id: str | None,
) -> list[tuple[str, int, float]]:
    with conn.cursor() as cur:
        if aoi_id is None:
            cur.execute(
                """
                SELECT block.id::text,
                       COUNT(bldg.id)::int,
                       ST_Area(block.geometry::geography)
                FROM curated_geo.feature block
                LEFT JOIN curated_geo.feature bldg
                       ON bldg.type_id = %s
                      AND block.geometry && bldg.geometry
                      AND ST_Within(ST_Centroid(bldg.geometry), block.geometry)
                WHERE block.type_id = %s
                GROUP BY block.id, block.geometry
                """,
                (building_type_id, block_type_id),
            )
        else:
            cur.execute(
                """
                SELECT block.id::text,
                       COUNT(bldg.id)::int,
                       ST_Area(block.geometry::geography)
                FROM curated_geo.feature block
                JOIN curated_geo.area_of_interest aoi
                  ON aoi.id = %s AND ST_Intersects(block.geometry, aoi.geometry)
                LEFT JOIN curated_geo.feature bldg
                       ON bldg.type_id = %s
                      AND block.geometry && bldg.geometry
                      AND ST_Within(ST_Centroid(bldg.geometry), block.geometry)
                WHERE block.type_id = %s
                GROUP BY block.id, block.geometry
                """,
                (aoi_id, building_type_id, block_type_id),
            )
        return [(str(fid), int(count), float(area)) for fid, count, area in cur.fetchall()]


def _fetch_block_building_counts_duckdb(
    conn,
    *,
    block_type_id: str,
    building_type_id: str,
    aoi_id: str | None,
) -> list[tuple[str, int, float]]:
    # UTM 14N area for CDMX — matches the compactness worker; accurate enough
    # at this latitude for a hectare-scale metric.
    if aoi_id is None:
        rows = conn.execute(
            """
            SELECT CAST(block.id AS VARCHAR),
                   COUNT(bldg.id),
                   ST_Area(ST_Transform(block.geometry, 'EPSG:4326', 'EPSG:32614', true))
            FROM curated_geo.feature block
            LEFT JOIN curated_geo.feature bldg
                   ON CAST(bldg.type_id AS VARCHAR) = ?
                  AND ST_Intersects(block.geometry, bldg.geometry)
                  AND ST_Within(ST_Centroid(bldg.geometry), block.geometry)
            WHERE CAST(block.type_id AS VARCHAR) = ?
            GROUP BY block.id, block.geometry
            """,
            [building_type_id, block_type_id],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT CAST(block.id AS VARCHAR),
                   COUNT(bldg.id),
                   ST_Area(ST_Transform(block.geometry, 'EPSG:4326', 'EPSG:32614', true))
            FROM curated_geo.feature block
            JOIN curated_geo.area_of_interest aoi
              ON CAST(aoi.id AS VARCHAR) = ?
              AND ST_Intersects(block.geometry, aoi.geometry)
            LEFT JOIN curated_geo.feature bldg
                   ON CAST(bldg.type_id AS VARCHAR) = ?
                  AND ST_Intersects(block.geometry, bldg.geometry)
                  AND ST_Within(ST_Centroid(bldg.geometry), block.geometry)
            WHERE CAST(block.type_id AS VARCHAR) = ?
            GROUP BY block.id, block.geometry
            """,
            [aoi_id, building_type_id, block_type_id],
        ).fetchall()
    return [(str(fid), int(count), float(area)) for fid, count, area in rows]
