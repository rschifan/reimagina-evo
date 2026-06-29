"""CLI helpers for the indicator layer."""

from __future__ import annotations

from reimagina.db import connect
from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.jobs.aoi_summary import summarise_aoi
from reimagina.jobs.rollup import run_rollup
from reimagina.orchestration.intake import evaluate_scenario


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
    if slug == "urban_form_block_building_density":
        with connect(dsn) as conn:
            result = compute_block_building_density(
                conn,
                aoi_id=aoi_id,
                scenario_id=scenario_id,
                code_version=code_version,
            )
        return {
            "dsn": dsn,
            "indicator_slug": result.indicator_slug,
            "job_run_id": result.job_run_id,
            "blocks_considered": result.blocks_considered,
            "buildings_attributed": result.buildings_attributed,
            "rows_written": result.rows_written,
            "aoi_id": result.aoi_id,
        }
    raise ValueError(
        f"Unknown indicator slug: {slug!r}. "
        "Known slugs: urban_form_block_compactness, urban_form_block_building_density."
    )


def rollup_indicator(
    *,
    dsn: str,
    slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, object]:
    """Roll an indicator up from a source grain to a coarser target grain."""
    with connect(dsn) as conn:
        result = run_rollup(
            conn,
            indicator_slug=slug,
            source_feature_type=source_feature_type,
            target_feature_type=target_feature_type,
            scenario_id=scenario_id,
            code_version=code_version,
        )
    return {
        "dsn": dsn,
        "indicator_slug": result.indicator_slug,
        "job_run_id": result.job_run_id,
        "source_feature_type": result.source_feature_type,
        "target_feature_type": result.target_feature_type,
        "aggregation_method": result.aggregation_method,
        "targets_considered": result.targets_considered,
        "rows_written": result.rows_written,
    }


def summarise_aoi_indicator(
    *,
    dsn: str,
    aoi_id: str,
    slug: str,
    scenario_id: str | None = None,
) -> dict[str, object]:
    """Compute and upsert one aoi_indicator_summary row."""
    with connect(dsn) as conn:
        result = summarise_aoi(
            conn,
            aoi_id=aoi_id,
            indicator_slug=slug,
            scenario_id=scenario_id,
        )
    return {
        "dsn": dsn,
        "aoi_id": result.aoi_id,
        "indicator_slug": result.indicator_slug,
        "scenario_id": result.scenario_id,
        "value": result.value,
        "status": result.status,
        "aggregation_note": result.aggregation_note,
    }


def evaluate_scenario_cli(
    *,
    dsn: str,
    changed_attributes: list[str],
    edited_features: list[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, object]:
    """Selectively recompute indicators for a scenario edit.

    Composes the impact engine + coordinator via the orchestration intake
    entry point and returns the per-unit run report.
    """
    with connect(dsn) as conn:
        report = evaluate_scenario(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
        )
    return {
        "dsn": dsn,
        "status": report.status,
        "outcomes": [
            {"indicator": o.indicator, "status": o.status.value, "detail": o.detail}
            for o in report.outcomes
        ],
    }
