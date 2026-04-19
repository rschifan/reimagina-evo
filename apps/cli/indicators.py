"""CLI helpers for the indicator layer."""
from __future__ import annotations

from reimagina.db import connect
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness


def seed_indicator_catalog(*, dsn: str) -> dict[str, object]:
    """Idempotently register every shipped indicator in indicator_catalog."""
    with connect(dsn) as conn:
        seeded = seed_catalog(conn)
    return {"dsn": dsn, "seeded": seeded, "count": len(seeded)}


def compute_indicator(
    *,
    dsn: str,
    slug: str,
    aoi_id: str | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, object]:
    """Dispatch to the concrete compute worker for `slug`."""
    if slug == "urban_form_block_compactness":
        with connect(dsn) as conn:
            result = compute_block_compactness(
                conn,
                aoi_id=aoi_id,
                scenario_id=scenario_id,
                code_version=code_version,
            )
        return {
            "dsn": dsn,
            "indicator_slug": result.indicator_slug,
            "job_run_id": result.job_run_id,
            "features_considered": result.features_considered,
            "rows_written": result.rows_written,
            "aoi_id": result.aoi_id,
        }
    raise ValueError(
        f"Unknown indicator slug: {slug!r}. "
        "Only 'urban_form_block_compactness' is wired today."
    )
