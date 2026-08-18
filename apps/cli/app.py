"""Developer CLI entrypoint for local bootstrap and first-source workflows."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

packages_dir = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(packages_dir) not in sys.path:
    sys.path.insert(0, str(packages_dir))

from reimagina.db import (  # noqa: E402
    DEFAULT_ADMIN_DSN,
    DEFAULT_APP_DSN,
    DEFAULT_TEST_DSN,
    bootstrap_local_platform,
    connect,
    verify_required_relations,
)
from reimagina.storage import mask_dsn  # noqa: E402
from reimagina.logging_setup import configure_logging  # noqa: E402
from reimagina.tiles.defaults import (  # noqa: E402
    tile_live_base_url,
    tile_snapshot_base_url,
)
from .explore import (  # noqa: E402
    list_executor_health,
    list_feature_stats,
    list_layers,
    list_serving_stats,
    list_sources,
)
from .overture import (  # noqa: E402
    as_json,
    extract_mexico_city_buildings,
    load_mexico_city_buildings,
    mexico_city_buildings_example,
    parse_bbox,
    validate_mexico_city_buildings,
)
from reimagina.pipeline.overture import (  # noqa: E402
    MEXICO_CITY_BBOX,
    OVERTURE_BUILDINGS_SOURCE_NAME,
    OVERTURE_DEFAULT_RELEASE,
)
from .perf import (  # noqa: E402
    compare_perf_reports,
    explain_query,
    performance_report,
    run_perf_benchmarks,
    seed_observations,
)
from .cdmx import (  # noqa: E402
    aoi_from_alcaldia,
    aoi_from_state,
    bootstrap_cdmx,
    cdmx_status,
    fetch_cdmx_layer,
)
from .feat import seed_types  # noqa: E402
from .aoi import compute_features as compute_aoi_features_cli  # noqa: E402
from .aoi import delete_aoi, list_aois  # noqa: E402
from .data import verify_sources  # noqa: E402
from .inegi import (  # noqa: E402
    bootstrap_inegi_censo,
    bootstrap_inegi_marco,
)
from .points import bootstrap_points  # noqa: E402
from .indicators import (  # noqa: E402
    compute_indicator,
    evaluate_scenario_cli,
    rollup_indicator,
    seed_indicator_catalog,
    summarise_aoi_indicator,
)
from .overlap import (  # noqa: E402
    compute_overlaps_all,
    declare_overlap_pair,
    list_overlap_pairs,
)
from .tiles import (  # noqa: E402
    export_tile_snapshot,
    list_tile_snapshots,
    list_tile_sources,
    tile_manifest,
)
from .raster import (  # noqa: E402
    PrefixCollisionError,
    add_derivation,
    fetch_worldpop_raster,
    ingest_raster,
    list_derivations,
    list_raster_layers,
    remove_derivation,
    run_derivation,
    run_zonal_stats,
    set_derivation_enabled,
    set_raster_published,
)


def create_cli() -> dict[str, str]:
    return {
        "app": "cli",
        "namespace": "reimagina",
        "status": "bootstrap-ready",
    }


def cli_examples() -> dict[str, dict[str, str]]:
    return {
        "overture_mexico_city_buildings": mexico_city_buildings_example(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reimagina-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show CLI scaffold status")
    status_parser.set_defaults(handler=_handle_status)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-db",
        help="Create local app/test DBs if needed and migrate both to head",
    )
    bootstrap_parser.add_argument("--app-dsn", default=DEFAULT_APP_DSN)
    bootstrap_parser.add_argument("--test-dsn", default=DEFAULT_TEST_DSN)
    bootstrap_parser.add_argument("--admin-dsn", default=DEFAULT_ADMIN_DSN)
    bootstrap_parser.set_defaults(handler=_handle_bootstrap_db)

    verify_parser = subparsers.add_parser(
        "verify-schema",
        help="Check that the canonical first-layer schemas/tables/views exist",
    )
    verify_parser.add_argument("--dsn", default=DEFAULT_APP_DSN)
    verify_parser.set_defaults(handler=_handle_verify_schema)

    explore_parser = subparsers.add_parser(
        "explore",
        help="Read-only inspection commands for the engineer-facing explore schema",
    )
    explore_subparsers = explore_parser.add_subparsers(
        dest="explore_command", required=True
    )

    explore_layers = explore_subparsers.add_parser(
        "layers", help="List the curated exploration catalog"
    )
    explore_layers.add_argument("--dsn", default=DEFAULT_APP_DSN)
    explore_layers.set_defaults(handler=_handle_explore_layers)

    explore_sources = explore_subparsers.add_parser(
        "sources",
        help="List source registry rows joined with ingestion runs",
    )
    explore_sources.add_argument("--dsn", default=DEFAULT_APP_DSN)
    explore_sources.set_defaults(handler=_handle_explore_sources)

    explore_feature_stats = explore_subparsers.add_parser(
        "feature-stats",
        help="Show canonical feature counts by type, role, source, and dataset version",
    )
    explore_feature_stats.add_argument("--dsn", default=DEFAULT_APP_DSN)
    explore_feature_stats.set_defaults(handler=_handle_explore_feature_stats)

    explore_serving_stats = explore_subparsers.add_parser(
        "serving-stats",
        help="Show high-level counts over serving_geo.feature_v",
    )
    explore_serving_stats.add_argument("--dsn", default=DEFAULT_APP_DSN)
    explore_serving_stats.set_defaults(handler=_handle_explore_serving_stats)

    explore_executor_health = explore_subparsers.add_parser(
        "executor-health",
        help="Show DBOS executor liveness from explore.ops_executor_health_v",
    )
    explore_executor_health.add_argument("--dsn", default=DEFAULT_APP_DSN)
    explore_executor_health.set_defaults(handler=_handle_explore_executor_health)

    overture_parser = subparsers.add_parser(
        "overture", help="First Overture source operations"
    )
    overture_subparsers = overture_parser.add_subparsers(
        dest="overture_command", required=True
    )

    overture_example = overture_subparsers.add_parser(
        "example",
        help="Show the pinned first-source example configuration",
    )
    overture_example.set_defaults(handler=_handle_overture_example)

    overture_extract = overture_subparsers.add_parser(
        "extract",
        help="Extract an Overture buildings subset to local GeoParquet",
    )
    overture_extract.add_argument(
        "--output-path",
        default="data/overture_buildings_mexico_city.parquet",
    )
    overture_extract.add_argument(
        "--release",
        default=OVERTURE_DEFAULT_RELEASE,
        help="Overture S3 release version (e.g. 2026-04-15.0)",
    )
    overture_extract.add_argument(
        "--bbox",
        default=",".join(str(v) for v in MEXICO_CITY_BBOX),
        help="Clip box xmin,ymin,xmax,ymax in EPSG:4326 (default: Mexico City)",
    )
    overture_extract.set_defaults(handler=_handle_overture_extract)

    overture_load = overture_subparsers.add_parser(
        "load",
        help="Register, stage, and load an Overture buildings extract",
    )
    overture_load.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overture_load.add_argument("--raw-path", required=True)
    overture_load.add_argument("--orchestrator-run-id")
    overture_load.add_argument("--code-version", default="dev")
    overture_load.add_argument(
        "--release",
        default=OVERTURE_DEFAULT_RELEASE,
        help="Overture S3 release version this parquet was extracted from "
        "(stored on data_source_catalog.version + feature.dataset_version)",
    )
    overture_load.add_argument(
        "--source-name",
        default=OVERTURE_BUILDINGS_SOURCE_NAME,
        help="data_source_catalog.name. Use a distinct value per AOI to "
        "avoid collapsing multiple bbox extracts onto the same source row",
    )
    overture_load.add_argument(
        "--aoi-id",
        help="Optional AOI uuid. When supplied, freshly-loaded buildings "
        "outside the AOI polygon are deleted post-load — required when "
        "the bbox is wider than the actual AOI",
    )
    overture_load.add_argument(
        "--recompute",
        action="store_true",
        help="After a successful load, submit an indicator run for the "
        "attributes it invalidated. Off by default: a city-scale load can "
        "invalidate every block-grain indicator, and those units are budgeted "
        "in minutes",
    )
    overture_load.add_argument(
        "--recompute-attributes",
        nargs="+",
        default=["geometry"],
        metavar="ATTR",
        help="Attribute names the load changed, matched against each "
        "indicator's declared `reads` (default: geometry). Only meaningful "
        "with --recompute",
    )
    overture_load.set_defaults(handler=_handle_overture_load)

    overture_validate = overture_subparsers.add_parser(
        "validate",
        help="Validate that loaded buildings are visible through serving_geo",
    )
    overture_validate.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overture_validate.set_defaults(handler=_handle_overture_validate)

    perf_parser = subparsers.add_parser(
        "perf",
        help="Performance benchmark and observability commands for Postgres/PostGIS",
    )
    perf_subparsers = perf_parser.add_subparsers(dest="perf_command", required=True)

    perf_seed = perf_subparsers.add_parser(
        "seed-observations",
        help="Seed deterministic observations and AOIs for benchmark tiers",
    )
    perf_seed.add_argument("--dsn", default=DEFAULT_APP_DSN)
    perf_seed.add_argument(
        "--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke"
    )
    perf_seed.add_argument("--feature-limit", type=int)
    perf_seed.set_defaults(handler=_handle_perf_seed_observations)

    perf_run = perf_subparsers.add_parser(
        "run",
        help="Run the named benchmark corpus and emit a structured JSON report",
    )
    perf_run.add_argument("--dsn", default=DEFAULT_APP_DSN)
    perf_run.add_argument(
        "--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke"
    )
    perf_run.add_argument("--benchmark", action="append", dest="benchmarks")
    perf_run.add_argument("--iterations", type=int)
    perf_run.add_argument("--output-path")
    perf_run.set_defaults(handler=_handle_perf_run)

    perf_explain = perf_subparsers.add_parser(
        "explain",
        help="Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for one benchmark",
    )
    perf_explain.add_argument("benchmark_name")
    perf_explain.add_argument("--dsn", default=DEFAULT_APP_DSN)
    perf_explain.add_argument(
        "--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke"
    )
    perf_explain.set_defaults(handler=_handle_perf_explain)

    perf_report = perf_subparsers.add_parser(
        "report",
        help="Show a live Postgres observability report for performance monitoring",
    )
    perf_report.add_argument("--dsn", default=DEFAULT_APP_DSN)
    perf_report.add_argument("--limit", type=int, default=10)
    perf_report.add_argument("--format", choices=["json"], default="json")
    perf_report.set_defaults(handler=_handle_perf_report)

    perf_compare = perf_subparsers.add_parser(
        "compare",
        help="Compare two benchmark result JSON files",
    )
    perf_compare.add_argument("baseline_path")
    perf_compare.add_argument("candidate_path")
    perf_compare.set_defaults(handler=_handle_perf_compare)

    tiles_parser = subparsers.add_parser(
        "tiles",
        help="Hybrid live-MVT and PMTiles snapshot commands",
    )
    tiles_subparsers = tiles_parser.add_subparsers(dest="tiles_command", required=True)

    tiles_manifest = tiles_subparsers.add_parser(
        "manifest",
        help="Show the infrastructure-owned layer manifest for live and snapshot tile sources",
    )
    tiles_manifest.add_argument("--manifest-path")
    tiles_manifest.set_defaults(handler=_handle_tiles_manifest)

    tiles_export = tiles_subparsers.add_parser(
        "export",
        help="Build a PMTiles snapshot for a configured stable layer",
    )
    tiles_export.add_argument("layer_id")
    tiles_export.add_argument("--dsn", default=DEFAULT_APP_DSN)
    tiles_export.add_argument("--output-dir", default="data/tiles/pmtiles")
    tiles_export.add_argument("--snapshot-base-url", default=tile_snapshot_base_url())
    tiles_export.add_argument("--manifest-path")
    tiles_export.set_defaults(handler=_handle_tiles_export)

    tiles_sources = tiles_subparsers.add_parser(
        "sources",
        help="Resolve frontend-facing layer source configs from the manifest and snapshot registry",
    )
    tiles_sources.add_argument("--dsn", default=DEFAULT_APP_DSN)
    tiles_sources.add_argument("--live-tilejson-base-url", default=tile_live_base_url())
    tiles_sources.add_argument("--snapshot-base-url", default=tile_snapshot_base_url())
    tiles_sources.add_argument("--manifest-path")
    tiles_sources.set_defaults(handler=_handle_tiles_sources)

    tiles_snapshots = tiles_subparsers.add_parser(
        "snapshots",
        help="List the PMTiles snapshot registry",
    )
    tiles_snapshots.add_argument("--dsn", default=DEFAULT_APP_DSN)
    tiles_snapshots.add_argument("--layer-id")
    tiles_snapshots.set_defaults(handler=_handle_tiles_snapshots)

    feat_parser = subparsers.add_parser(
        "feat",
        help="Canonical feature_type_catalog operations",
    )
    feat_subparsers = feat_parser.add_subparsers(dest="feat_command", required=True)
    feat_seed = feat_subparsers.add_parser(
        "seed-types",
        help="Seed the MEX + global feature types (idempotent)",
    )
    feat_seed.add_argument("--dsn", default=DEFAULT_APP_DSN)
    feat_seed.set_defaults(handler=_handle_feat_seed_types)

    cdmx_parser = subparsers.add_parser(
        "cdmx",
        help="CDMX canonical-geometry bootstrap (INEGI + datos.cdmx.gob.mx)",
    )
    cdmx_subparsers = cdmx_parser.add_subparsers(dest="cdmx_command", required=True)

    cdmx_fetch = cdmx_subparsers.add_parser(
        "fetch",
        help="Download (or reuse cached) archive for one CDMX layer",
    )
    cdmx_fetch.add_argument(
        "--layer",
        required=True,
        choices=("municipalities", "agebs", "blocks", "colonias"),
    )
    cdmx_fetch.add_argument("--overwrite", action="store_true")
    cdmx_fetch.set_defaults(handler=_handle_cdmx_fetch)

    cdmx_bootstrap = cdmx_subparsers.add_parser(
        "bootstrap",
        help="Fetch + stage + load CDMX layers into curated_geo.feature",
    )
    cdmx_bootstrap.add_argument("--dsn", default=DEFAULT_APP_DSN)
    cdmx_bootstrap.add_argument(
        "--layers",
        help="Comma-separated layer keys; default is municipalities,agebs,blocks,colonias",
    )
    cdmx_bootstrap.add_argument(
        "--from-path",
        action="append",
        default=[],
        metavar="LAYER=PATH",
        help="Use a local archive instead of downloading, e.g. blocks=/path/to/09_ciudaddemexico.zip. Repeatable.",
    )
    cdmx_bootstrap.add_argument("--code-version", default="dev")
    cdmx_bootstrap.set_defaults(handler=_handle_cdmx_bootstrap)

    cdmx_status_cmd = cdmx_subparsers.add_parser(
        "status",
        help="Row counts per MEX feature type",
    )
    cdmx_status_cmd.add_argument("--dsn", default=DEFAULT_APP_DSN)
    cdmx_status_cmd.set_defaults(handler=_handle_cdmx_status)

    cdmx_aoi = cdmx_subparsers.add_parser(
        "aoi-from-alcaldia",
        help="Create an AOI whose geometry is the matching MEX:municipality polygon",
    )
    cdmx_aoi.add_argument("--dsn", default=DEFAULT_APP_DSN)
    cdmx_aoi.add_argument(
        "--cve-ent",
        default="09",
        help="INEGI state code; default 09 (CDMX). Use 02 for Mexicali.",
    )
    cdmx_aoi.add_argument(
        "--cve-mun",
        required=True,
        help="INEGI municipality code, e.g. 015 for Cuauhtémoc, 002 for Mexicali",
    )
    cdmx_aoi.add_argument("--name", required=True)
    cdmx_aoi.add_argument("--project-name", default="CDMX Realistic-Scale Validation")
    cdmx_aoi.set_defaults(handler=_handle_cdmx_aoi_from_alcaldia)

    cdmx_aoi_state = cdmx_subparsers.add_parser(
        "aoi-from-state",
        help=(
            "Create an AOI whose geometry is the entire entidad (state) "
            "polygon — e.g. 'all of CDMX' as one AOI."
        ),
    )
    cdmx_aoi_state.add_argument("--dsn", default=DEFAULT_APP_DSN)
    cdmx_aoi_state.add_argument(
        "--cve-ent",
        required=True,
        help="INEGI state code, e.g. 09 for CDMX, 02 for Baja California.",
    )
    cdmx_aoi_state.add_argument("--name", required=True)
    cdmx_aoi_state.add_argument(
        "--project-name", default="CDMX Realistic-Scale Validation"
    )
    cdmx_aoi_state.set_defaults(handler=_handle_cdmx_aoi_from_state)

    inegi_marco_parser = subparsers.add_parser(
        "inegi-marco",
        help="INEGI Marco Geoestadístico 2024 ingest from the data-lake mirror",
    )
    inegi_marco_subparsers = inegi_marco_parser.add_subparsers(
        dest="inegi_marco_command",
        required=True,
    )

    inegi_marco_bootstrap = inegi_marco_subparsers.add_parser(
        "bootstrap",
        help="Stage + load INEGI marco layers (filtered by CVE_ENT) into curated_geo.feature",
    )
    inegi_marco_bootstrap.add_argument("--dsn", default=DEFAULT_APP_DSN)
    inegi_marco_bootstrap.add_argument(
        "--layers",
        help=(
            "Comma-separated layer keys. Default: all four "
            "(areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas)."
        ),
    )
    inegi_marco_bootstrap.add_argument(
        "--cve-ent",
        action="append",
        default=[],
        metavar="CVE_ENT",
        help=(
            "INEGI entidad code (e.g. 02 for Baja California, 09 for CDMX). "
            "Repeatable. If omitted, ingests ALL of Mexico — usually not what you want."
        ),
    )
    inegi_marco_bootstrap.add_argument(
        "--source-root",
        help=(
            "Override the source directory (defaults to "
            "data/sources/azure/inegi/marco_geostadistico/2024)."
        ),
    )
    inegi_marco_bootstrap.add_argument("--code-version", default="dev")
    inegi_marco_bootstrap.set_defaults(handler=_handle_inegi_marco_bootstrap)

    inegi_censo_parser = subparsers.add_parser(
        "inegi-censo",
        help="INEGI 2020 census ingest (manzana-level → curated_geo.observation)",
    )
    inegi_censo_subparsers = inegi_censo_parser.add_subparsers(
        dest="inegi_censo_command",
        required=True,
    )
    inegi_censo_bootstrap = inegi_censo_subparsers.add_parser(
        "bootstrap",
        help="Read filtered 2020 census parquet, join to features, write observations",
    )
    inegi_censo_bootstrap.add_argument("--dsn", default=DEFAULT_APP_DSN)
    inegi_censo_bootstrap.add_argument(
        "--cve-ent",
        action="append",
        default=[],
        metavar="CVE_ENT",
        help=(
            "INEGI entidad code, e.g. 02 or 9 (auto-normalised). "
            "Repeatable. If omitted, ingests ALL of Mexico — usually "
            "not what you want."
        ),
    )
    inegi_censo_bootstrap.add_argument(
        "--columns",
        help=(
            "Comma-separated census column names. Default: headline subset "
            "(pobtot, pobmas, pobfem, p_15ymas, p_18ymas, vivtot)."
        ),
    )
    inegi_censo_bootstrap.add_argument(
        "--source-root",
        help=(
            "Override the source directory (defaults to "
            "data/sources/azure/inegi/censo/manzanas_2020.parquet)."
        ),
    )
    inegi_censo_bootstrap.add_argument("--code-version", default="dev")
    inegi_censo_bootstrap.set_defaults(handler=_handle_inegi_censo_bootstrap)

    data_parser = subparsers.add_parser(
        "data",
        help="Cross-source operations (smoke tests, manifests)",
    )
    data_subparsers = data_parser.add_subparsers(dest="data_command", required=True)

    data_verify = data_subparsers.add_parser(
        "verify-sources",
        help="Check that every upstream source listed in the bootstrap "
        "runbook is reachable before triggering a long ingest",
    )
    data_verify.add_argument(
        "--release",
        default=OVERTURE_DEFAULT_RELEASE,
        help="Overture release to probe on S3",
    )
    data_verify.add_argument(
        "--worldpop-country",
        default="MEX",
        help="ISO3 country code used to probe WorldPop's REST catalogue. "
        "Default MEX so the smoke test mirrors the runbook.",
    )
    data_verify.set_defaults(handler=_handle_data_verify_sources)

    aoi_parser = subparsers.add_parser(
        "aoi",
        help="Area-of-interest lifecycle: list, delete (creators live under cdmx)",
    )
    aoi_subparsers = aoi_parser.add_subparsers(dest="aoi_command", required=True)

    aoi_list = aoi_subparsers.add_parser(
        "list", help="List every AOI with bbox + feature count"
    )
    aoi_list.add_argument("--dsn", default=DEFAULT_APP_DSN)
    aoi_list.set_defaults(handler=_handle_aoi_list)

    aoi_delete = aoi_subparsers.add_parser(
        "delete",
        help="Delete an AOI by uuid. CASCADE removes aoi_feature rows; "
        "features themselves are preserved",
    )
    aoi_delete.add_argument("--dsn", default=DEFAULT_APP_DSN)
    aoi_delete.add_argument("--aoi-id", required=True)
    aoi_delete.set_defaults(handler=_handle_aoi_delete)

    aoi_compute = aoi_subparsers.add_parser(
        "compute-features",
        help="(Re)populate aoi_feature for one or all AOIs. Idempotent.",
    )
    aoi_compute.add_argument("--dsn", default=DEFAULT_APP_DSN)
    aoi_compute.add_argument(
        "--aoi-id",
        help="Optional. Limit to a single AOI uuid; default runs every AOI.",
    )
    aoi_compute.set_defaults(handler=_handle_aoi_compute_features)

    points_parser = subparsers.add_parser(
        "points",
        help="Per-pilot-city amenity / POI ingest from the data-lake mirror",
    )
    points_subparsers = points_parser.add_subparsers(
        dest="points_command",
        required=True,
    )

    points_bootstrap = points_subparsers.add_parser(
        "bootstrap",
        help="Stage + load amenity GeoParquet layers into curated_geo.feature",
    )
    points_bootstrap.add_argument("--dsn", default=DEFAULT_APP_DSN)
    points_bootstrap.add_argument(
        "--layers",
        help=(
            "Comma-separated layer keys. Default: all "
            "(tlalpan_amenities,mexicali_amenities)."
        ),
    )
    points_bootstrap.add_argument(
        "--source-root",
        help="Override the source directory (defaults to data/sources/azure).",
    )
    points_bootstrap.add_argument("--code-version", default="dev")
    points_bootstrap.set_defaults(handler=_handle_points_bootstrap)

    indicators_parser = subparsers.add_parser(
        "indicators",
        help="Indicator catalog management and compute dispatch",
    )
    indicators_subparsers = indicators_parser.add_subparsers(
        dest="indicators_command", required=True
    )
    indicators_seed = indicators_subparsers.add_parser(
        "seed-catalog",
        help="Register every shipped indicator in curated_geo.indicator_catalog",
    )
    indicators_seed.add_argument("--dsn", default=DEFAULT_APP_DSN)
    indicators_seed.set_defaults(handler=_handle_indicators_seed_catalog)

    indicators_compute = indicators_subparsers.add_parser(
        "compute",
        help="Run the compute worker for a single indicator slug",
    )
    indicators_compute.add_argument("--dsn", default=DEFAULT_APP_DSN)
    indicators_compute.add_argument("--slug", required=True)
    indicators_compute.add_argument("--aoi-id")
    indicators_compute.add_argument("--scenario-id")
    indicators_compute.add_argument("--code-version", default="dev")
    indicators_compute.set_defaults(handler=_handle_indicators_compute)

    indicators_rollup = indicators_subparsers.add_parser(
        "rollup",
        help="Roll an indicator up from a source grain to a coarser target grain",
    )
    indicators_rollup.add_argument("--dsn", default=DEFAULT_APP_DSN)
    indicators_rollup.add_argument("--slug", required=True)
    indicators_rollup.add_argument("--source-type", required=True)
    indicators_rollup.add_argument("--target-type", required=True)
    indicators_rollup.add_argument("--scenario-id")
    indicators_rollup.add_argument("--code-version", default="dev")
    indicators_rollup.set_defaults(handler=_handle_indicators_rollup)

    indicators_summary = indicators_subparsers.add_parser(
        "aoi-summary",
        help="Compute one aoi_indicator_summary row for (aoi, indicator, scenario)",
    )
    indicators_summary.add_argument("--dsn", default=DEFAULT_APP_DSN)
    indicators_summary.add_argument("--aoi-id", required=True)
    indicators_summary.add_argument("--slug", required=True)
    indicators_summary.add_argument("--scenario-id")
    indicators_summary.set_defaults(handler=_handle_indicators_aoi_summary)

    evaluate_parser = subparsers.add_parser(
        "evaluate-scenario",
        help=(
            "Selectively recompute indicators for a scenario edit "
            "(impact engine → coordinator)."
        ),
    )
    evaluate_parser.add_argument("--dsn", default=DEFAULT_APP_DSN)
    evaluate_parser.add_argument(
        "--changed-attribute",
        action="append",
        default=[],
        metavar="ATTR",
        help="Attribute key the scenario changed (repeatable); seeds the dirty-set.",
    )
    evaluate_parser.add_argument(
        "--feature",
        action="append",
        default=[],
        metavar="FEATURE_ID",
        help="Feature id the change applies to (repeatable).",
    )
    evaluate_parser.add_argument("--aoi-id")
    evaluate_parser.add_argument("--scenario-id")
    evaluate_parser.add_argument("--code-version", default="dev")
    evaluate_parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for the run to finish (default 600; ignored for "
        "--detach / --sync). Unit concurrency is a property of the ops-plane "
        "queue (REIMAGINA_UNIT_CONCURRENCY), not of this command.",
    )
    # Default submits a durable run and waits for it. No standing worker is
    # needed — the ops-plane is a library, so this process drives the run it
    # submitted (needs DATABASE_URL_DIRECT). The two flags opt out — mutually
    # exclusive.
    mode_group = evaluate_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--detach",
        dest="detach",
        action="store_true",
        help="Enqueue and return immediately for a standalone "
        "`python -m apps.worker` fleet to drain (fire-and-forget; needs "
        "DATABASE_URL_DIRECT).",
    )
    mode_group.add_argument(
        "--sync",
        dest="sync",
        action="store_true",
        help="Run the in-process orchestrator (no queue, no worker).",
    )
    evaluate_parser.set_defaults(handler=_handle_evaluate_scenario)

    overlap_parser = subparsers.add_parser(
        "overlap",
        help="Feature-to-feature overlap pair declaration and compute",
    )
    overlap_subparsers = overlap_parser.add_subparsers(
        dest="overlap_command", required=True
    )

    overlap_declare = overlap_subparsers.add_parser(
        "declare-pair",
        help="Register a source→target type pair in feature_overlap_type_pair",
    )
    overlap_declare.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overlap_declare.add_argument("--source-context", required=True)
    overlap_declare.add_argument("--source-slug", required=True)
    overlap_declare.add_argument("--target-context", required=True)
    overlap_declare.add_argument("--target-slug", required=True)
    overlap_declare.set_defaults(handler=_handle_overlap_declare)

    overlap_list = overlap_subparsers.add_parser(
        "list-pairs",
        help="List declared type-pair rows",
    )
    overlap_list.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overlap_list.set_defaults(handler=_handle_overlap_list)

    overlap_compute = overlap_subparsers.add_parser(
        "compute-all",
        help="Compute overlaps for every declared pair (AOI-scoped recommended)",
    )
    overlap_compute.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overlap_compute.add_argument("--aoi-id")
    overlap_compute.set_defaults(handler=_handle_overlap_compute_all)

    # ── raster ─────────────────────────────────────────────────────────
    raster_parser = subparsers.add_parser(
        "raster",
        help="Single-band raster ingest + registry operations",
    )
    raster_subparsers = raster_parser.add_subparsers(
        dest="raster_command", required=True
    )

    raster_ingest = raster_subparsers.add_parser(
        "ingest",
        help="Register + stage (bronze → silver COG) a single-band raster, "
        "then insert a raster_layer row so /tile-sources picks it up",
    )
    raster_ingest.add_argument("--dsn", default=DEFAULT_APP_DSN)
    raster_ingest.add_argument(
        "--raw-path",
        required=True,
        help="Path to the raw single-band GeoTIFF / COG / NetCDF. "
        "Multi-band containers (HDF, multi-band NetCDF) are out of v1 "
        "scope — pre-extract one band with `gdal_translate -b <N>`.",
    )
    raster_ingest.add_argument(
        "--source-name", required=True, help='e.g. "MODIS LST 2026-01-15 day"'
    )
    raster_ingest.add_argument("--provider", required=True, help='e.g. "NASA LAADS"')
    raster_ingest.add_argument(
        "--version",
        required=True,
        help="Data version (e.g. acquisition date); rides along on source + asset rows",
    )
    raster_ingest.add_argument(
        "--layer-slug",
        required=True,
        help="Stable slug for the raster_layer row (e.g. lst_day_2026_01_15)",
    )
    raster_ingest.add_argument(
        "--aggregation-kind",
        choices=["intensive", "extensive"],
        default="intensive",
        help="Drives default zonal-stats; use 'extensive' for count/sum-per-pixel rasters",
    )
    raster_ingest.add_argument("--logical-name")
    raster_ingest.add_argument("--description")
    raster_ingest.add_argument("--colormap-name", help='e.g. "inferno"')
    raster_ingest.add_argument("--rescale", help='e.g. "270,320" (min,max)')
    raster_ingest.add_argument("--license")
    raster_ingest.add_argument(
        "--captured-at", help="ISO-8601 timestamp; stored on file_asset.captured_at"
    )
    raster_ingest.add_argument("--minzoom", type=int, default=5)
    raster_ingest.add_argument("--maxzoom", type=int, default=14)
    raster_ingest.add_argument(
        "--unpublished",
        action="store_true",
        help="Register the raster but hide it from /tile-sources until "
        "`raster publish --slug <slug>` flips it on",
    )
    raster_ingest.add_argument("--code-version", default="dev")
    raster_ingest.add_argument("--orchestrator-run-id")
    raster_ingest.set_defaults(handler=_handle_raster_ingest)

    raster_fetch_worldpop = raster_subparsers.add_parser(
        "fetch-worldpop",
        help="Download a WorldPop country raster from the public REST API "
        "(cached) and optionally clip it to an AOI polygon. Output is "
        "a local GeoTIFF ready for `raster ingest --raw-path`.",
    )
    raster_fetch_worldpop.add_argument(
        "--country",
        required=True,
        help="ISO3 country code, e.g. MEX. Used to query "
        "https://www.worldpop.org/rest/data/pop/<dataset>?iso3=<C>",
    )
    raster_fetch_worldpop.add_argument(
        "--year",
        required=True,
        type=int,
        help="Population year (e.g. 2020). Must match a popyear row in "
        "the WorldPop catalogue for the chosen country + dataset.",
    )
    raster_fetch_worldpop.add_argument(
        "--output-path",
        required=True,
        help="Where to write the GeoTIFF the operator passes to "
        "`raster ingest --raw-path` (e.g. data/worldpop_cdmx.tif)",
    )
    raster_fetch_worldpop.add_argument(
        "--cache-dir",
        default=None,
        help="Where to cache the country-scale GeoTIFF so re-running "
        "against another AOI doesn't re-download. Defaults to "
        "data/cache/worldpop/.",
    )
    raster_fetch_worldpop.add_argument(
        "--dataset",
        default="wpgp",
        help="WorldPop dataset slug (default 'wpgp' = per-pixel population).",
    )
    raster_fetch_worldpop.add_argument(
        "--aoi-id",
        help="When set, clip the country raster to this AOI's polygon "
        "(read from curated_geo.area_of_interest). Without --aoi-id "
        "the country raster is copied through verbatim — fine for "
        "small countries but ~1 GB for Mexico.",
    )
    raster_fetch_worldpop.add_argument(
        "--dsn",
        default=DEFAULT_APP_DSN,
        help="DSN used to read the AOI polygon when --aoi-id is given.",
    )
    raster_fetch_worldpop.add_argument(
        "--force",
        action="store_true",
        help="Re-download the country raster even if cached. Use only "
        "when a previous fetch finished partial / corrupt.",
    )
    raster_fetch_worldpop.set_defaults(handler=_handle_raster_fetch_worldpop)

    raster_list = raster_subparsers.add_parser(
        "list",
        help="List every registered raster layer (reads explore.raster_layer_v)",
    )
    raster_list.add_argument("--dsn", default=DEFAULT_APP_DSN)
    raster_list.set_defaults(handler=_handle_raster_list)

    raster_publish = raster_subparsers.add_parser(
        "publish",
        help="Flip the published flag on a raster_layer row by layer_slug",
    )
    raster_publish.add_argument("--dsn", default=DEFAULT_APP_DSN)
    raster_publish.add_argument("--layer-slug", required=True)
    raster_publish.add_argument(
        "--unpublish",
        action="store_true",
        help="Set published=false instead of true (default: publish)",
    )
    raster_publish.set_defaults(handler=_handle_raster_publish)

    raster_zonal = raster_subparsers.add_parser(
        "zonal-stats",
        help="Compute zonal stats on a raster for every feature of a type, "
        "writing one observation row per (feature, statistic)",
    )
    raster_zonal.add_argument("--dsn", default=DEFAULT_APP_DSN)
    raster_zonal.add_argument(
        "--source-id",
        required=True,
        help="UUID of the raster source (from `raster ingest` output or "
        "curated_geo.data_source_catalog)",
    )
    raster_zonal.add_argument(
        "--feature-type",
        required=True,
        help="Feature type slug (e.g. 'block', 'ageb') — must be a MEX "
        "type registered in curated_geo.feature_type_catalog",
    )
    raster_zonal.add_argument(
        "--attribute-key-prefix",
        required=True,
        help="Prefix for the observation attribute_key (e.g. 'lst_day'); "
        "stats land as '<prefix>_<stat>' (e.g. 'lst_day_mean')",
    )
    raster_zonal.add_argument(
        "--aoi-id",
        help="Restrict to features intersecting this AOI; omit for all features of the type",
    )
    raster_zonal.add_argument(
        "--coverage-threshold",
        type=float,
        default=0.0,
        help="Skip features whose fractional valid-pixel count is below this. "
        "Default 0.0 — write a row for every polygon that overlaps any "
        "non-nodata cell. Under exactextract's area-weighting, sub-pixel "
        "polygons (manzanas < 1 ha) get fractional coverage 0..1; values "
        ">0 still produce correct area-weighted sums. Raise (e.g. 0.5) "
        "only when explicitly excluding partially-covered polygons.",
    )
    raster_zonal.add_argument(
        "--statistics",
        help="Comma-separated stat names (e.g. 'mean,median,p05,p95'); "
        "omit to use the aggregation_kind default from raster_layer",
    )
    raster_zonal.add_argument("--code-version", default="dev")
    raster_zonal.add_argument(
        "--force",
        action="store_true",
        help="Override the prefix-collision check; use only when you have "
        "verified that co-ownership of the prefix is intentional",
    )
    raster_zonal.add_argument(
        "--direct-dsn",
        help="Postgres-only: direct-port DSN for the advisory-lock "
        "transaction. Defaults to $DATABASE_URL_DIRECT.",
    )
    raster_zonal.set_defaults(handler=_handle_raster_zonal_stats)

    # ── raster derivation (aggregation recipes, r020) ────────────────────
    derivation_parser = raster_subparsers.add_parser(
        "derivation",
        help="Declarative aggregation recipes: raster→observation and "
        "observation→observation derivations per target feature grain",
    )
    derivation_subparsers = derivation_parser.add_subparsers(
        dest="derivation_command",
        required=True,
    )

    deriv_add = derivation_subparsers.add_parser(
        "add",
        help="Register a recipe (raster or observation input). Use 'run' "
        "to materialise the observations it produces.",
    )
    deriv_add.add_argument("--dsn", default=DEFAULT_APP_DSN)
    deriv_add.add_argument(
        "--input-kind", choices=["raster", "observation"], required=True
    )
    deriv_add.add_argument(
        "--raster-layer-slug", help="Required if --input-kind=raster"
    )
    deriv_add.add_argument(
        "--input-prefix",
        help="Required if --input-kind=observation (the attribute_key_prefix on the source observations)",
    )
    deriv_add.add_argument(
        "--input-feature-type",
        help="Required if --input-kind=observation (grain of the source observations)",
    )
    deriv_add.add_argument(
        "--target-feature-type",
        required=True,
        help="Feature type slug of the output grain (e.g. 'municipality')",
    )
    deriv_add.add_argument(
        "--attribute-key-prefix",
        required=True,
        help="Output prefix — one observation per (feature, <prefix>_<suffix>)",
    )
    deriv_add.add_argument(
        "--method",
        required=True,
        choices=["mean", "weighted_mean", "sum", "min", "max"],
    )
    deriv_add.add_argument(
        "--weight-basis",
        choices=["area", "length", "presence"],
        help="Required with --method=weighted_mean for observation inputs",
    )
    deriv_add.add_argument(
        "--params",
        help='JSON blob of method-specific params (e.g. \'{"stats":["sum","count"]}\' '
        "for raster inputs that want multiple rasterstats primitives)",
    )
    deriv_add.add_argument("--coverage-threshold", type=float, default=0.5)
    deriv_add.add_argument(
        "--aoi-id", help="Restrict recipe execution to features inside this AOI"
    )
    deriv_add.add_argument(
        "--disabled",
        action="store_true",
        help="Register the recipe in a disabled state; enable later with "
        "`derivation enable`",
    )
    deriv_add.set_defaults(handler=_handle_derivation_add)

    deriv_list = derivation_subparsers.add_parser(
        "list",
        help="Show recipes (reads serving_geo.aggregation_recipe_v)",
    )
    deriv_list.add_argument("--dsn", default=DEFAULT_APP_DSN)
    deriv_list.add_argument(
        "--raster-layer-slug", help="Filter to recipes whose input is this raster layer"
    )
    deriv_list.add_argument("--include-disabled", action="store_true")
    deriv_list.set_defaults(handler=_handle_derivation_list)

    deriv_enable = derivation_subparsers.add_parser(
        "enable",
        help="Flip enabled=true on a recipe (opposite: `derivation disable`)",
    )
    deriv_enable.add_argument("--dsn", default=DEFAULT_APP_DSN)
    deriv_enable.add_argument("--recipe-id", required=True)
    deriv_enable.add_argument(
        "--disable", action="store_true", help="Set enabled=false instead of true"
    )
    deriv_enable.set_defaults(handler=_handle_derivation_enable)

    deriv_remove = derivation_subparsers.add_parser(
        "remove",
        help="Hard-delete a recipe. Existing observations stay put; "
        "retire them separately if wanted.",
    )
    deriv_remove.add_argument("--dsn", default=DEFAULT_APP_DSN)
    deriv_remove.add_argument("--recipe-id", required=True)
    deriv_remove.set_defaults(handler=_handle_derivation_remove)

    deriv_run = derivation_subparsers.add_parser(
        "run",
        help="Materialise one recipe by id, OR every enabled recipe for a raster layer",
    )
    deriv_run.add_argument("--dsn", default=DEFAULT_APP_DSN)
    deriv_run.add_argument("--recipe-id", help="UUID of a single recipe to run")
    deriv_run.add_argument(
        "--raster-layer-slug",
        help="Slug of a raster layer; runs every enabled recipe whose "
        "input is that layer",
    )
    deriv_run.add_argument(
        "--direct-dsn",
        help="Postgres-only: direct-port DSN used by the critical section's "
        "dedicated connection. Defaults to $DATABASE_URL_DIRECT.",
    )
    deriv_run.set_defaults(handler=_handle_derivation_run)

    return parser


def _print(data: dict[str, Any]) -> int:
    # Never echo a DSN password into stdout/logs.
    if isinstance(data.get("dsn"), str):
        data = {**data, "dsn": mask_dsn(data["dsn"])}
    print(as_json(data))
    return 0


def _handle_status(_args: argparse.Namespace) -> int:
    return _print(
        {
            **create_cli(),
            "examples": cli_examples(),
        }
    )


def _handle_bootstrap_db(args: argparse.Namespace) -> int:
    return _print(
        bootstrap_local_platform(
            app_dsn=args.app_dsn,
            test_dsn=args.test_dsn,
            admin_dsn=args.admin_dsn,
        )
    )


def _handle_verify_schema(args: argparse.Namespace) -> int:
    with connect(args.dsn) as conn:
        checks = verify_required_relations(conn)
    return _print(
        {"dsn": args.dsn, "checks": checks, "all_present": all(checks.values())}
    )


def _handle_overture_example(_args: argparse.Namespace) -> int:
    return _print(mexico_city_buildings_example())


def _handle_explore_layers(args: argparse.Namespace) -> int:
    return _print(list_layers(dsn=args.dsn))


def _handle_explore_sources(args: argparse.Namespace) -> int:
    return _print(list_sources(dsn=args.dsn))


def _handle_explore_feature_stats(args: argparse.Namespace) -> int:
    return _print(list_feature_stats(dsn=args.dsn))


def _handle_explore_serving_stats(args: argparse.Namespace) -> int:
    return _print(list_serving_stats(dsn=args.dsn))


def _handle_explore_executor_health(args: argparse.Namespace) -> int:
    return _print(list_executor_health(dsn=args.dsn))


def _handle_overture_extract(args: argparse.Namespace) -> int:
    return _print(
        extract_mexico_city_buildings(
            output_path=args.output_path,
            release=args.release,
            bbox=parse_bbox(args.bbox),
        )
    )


def _handle_overture_load(args: argparse.Namespace) -> int:
    return _print(
        load_mexico_city_buildings(
            dsn=args.dsn,
            raw_path=args.raw_path,
            orchestrator_run_id=args.orchestrator_run_id,
            code_version=args.code_version,
            release=args.release,
            source_name=args.source_name,
            aoi_id=args.aoi_id,
            recompute=args.recompute,
            recompute_attributes=tuple(args.recompute_attributes),
        )
    )


def _handle_overture_validate(args: argparse.Namespace) -> int:
    return _print(validate_mexico_city_buildings(dsn=args.dsn))


def _handle_perf_seed_observations(args: argparse.Namespace) -> int:
    return _print(
        seed_observations(
            dsn=args.dsn,
            dataset_tier=args.dataset_tier,
            feature_limit=args.feature_limit,
        )
    )


def _handle_perf_run(args: argparse.Namespace) -> int:
    return _print(
        run_perf_benchmarks(
            dsn=args.dsn,
            dataset_tier=args.dataset_tier,
            benchmark_names=args.benchmarks,
            iterations=args.iterations,
            output_path=args.output_path,
        )
    )


def _handle_perf_explain(args: argparse.Namespace) -> int:
    return _print(
        explain_query(
            dsn=args.dsn,
            benchmark_name=args.benchmark_name,
            dataset_tier=args.dataset_tier,
        )
    )


def _handle_perf_report(args: argparse.Namespace) -> int:
    return _print(
        performance_report(
            dsn=args.dsn,
            limit=args.limit,
        )
    )


def _handle_perf_compare(args: argparse.Namespace) -> int:
    return _print(
        compare_perf_reports(
            baseline_path=args.baseline_path,
            candidate_path=args.candidate_path,
        )
    )


def _handle_tiles_manifest(args: argparse.Namespace) -> int:
    return _print(tile_manifest(manifest_path=args.manifest_path))


def _handle_tiles_export(args: argparse.Namespace) -> int:
    return _print(
        export_tile_snapshot(
            dsn=args.dsn,
            layer_id=args.layer_id,
            output_dir=args.output_dir,
            snapshot_base_url=args.snapshot_base_url,
            manifest_path=args.manifest_path,
        )
    )


def _handle_tiles_sources(args: argparse.Namespace) -> int:
    return _print(
        list_tile_sources(
            dsn=args.dsn,
            live_tilejson_base_url=args.live_tilejson_base_url,
            snapshot_base_url=args.snapshot_base_url,
            manifest_path=args.manifest_path,
        )
    )


def _handle_tiles_snapshots(args: argparse.Namespace) -> int:
    return _print(
        list_tile_snapshots(
            dsn=args.dsn,
            layer_id=args.layer_id,
        )
    )


def _handle_feat_seed_types(args: argparse.Namespace) -> int:
    return _print(seed_types(dsn=args.dsn))


def _handle_cdmx_fetch(args: argparse.Namespace) -> int:
    payload = fetch_cdmx_layer(layer=args.layer, overwrite=args.overwrite)
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


def _parse_from_path_pairs(raw: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            raise SystemExit(f"--from-path expects LAYER=PATH, got {entry!r}")
        key, path = entry.split("=", 1)
        pairs[key.strip()] = path.strip()
    return pairs


def _handle_cdmx_bootstrap(args: argparse.Namespace) -> int:
    layers = [k.strip() for k in args.layers.split(",")] if args.layers else None
    from_paths = _parse_from_path_pairs(args.from_path)
    return _print(
        bootstrap_cdmx(
            dsn=args.dsn,
            layers=layers,
            from_paths=from_paths,
            code_version=args.code_version,
        )
    )


def _handle_cdmx_status(args: argparse.Namespace) -> int:
    return _print(cdmx_status(dsn=args.dsn))


def _handle_inegi_marco_bootstrap(args: argparse.Namespace) -> int:
    layers = [k.strip() for k in args.layers.split(",")] if args.layers else None
    cve_ent_filter = list(args.cve_ent) if args.cve_ent else None
    return _print(
        bootstrap_inegi_marco(
            dsn=args.dsn,
            layers=layers,
            cve_ent_filter=cve_ent_filter,
            source_root=args.source_root,
            code_version=args.code_version,
        )
    )


def _handle_inegi_censo_bootstrap(args: argparse.Namespace) -> int:
    columns = [c.strip() for c in args.columns.split(",")] if args.columns else None
    cve_ent_filter = list(args.cve_ent) if args.cve_ent else None
    return _print(
        bootstrap_inegi_censo(
            dsn=args.dsn,
            cve_ent_filter=cve_ent_filter,
            columns=columns,
            source_root=args.source_root,
            code_version=args.code_version,
        )
    )


def _handle_points_bootstrap(args: argparse.Namespace) -> int:
    layers = [k.strip() for k in args.layers.split(",")] if args.layers else None
    return _print(
        bootstrap_points(
            dsn=args.dsn,
            layers=layers,
            source_root=args.source_root,
            code_version=args.code_version,
        )
    )


def _handle_aoi_list(args: argparse.Namespace) -> int:
    return _print(list_aois(dsn=args.dsn))


def _handle_aoi_delete(args: argparse.Namespace) -> int:
    return _print(delete_aoi(dsn=args.dsn, aoi_id=args.aoi_id))


def _handle_aoi_compute_features(args: argparse.Namespace) -> int:
    return _print(compute_aoi_features_cli(dsn=args.dsn, aoi_id=args.aoi_id))


def _handle_data_verify_sources(args: argparse.Namespace) -> int:
    result = verify_sources(
        release=args.release,
        worldpop_country=args.worldpop_country,
    )
    _print(result)
    return 0 if result["all_ok"] else 1


def _handle_cdmx_aoi_from_alcaldia(args: argparse.Namespace) -> int:
    payload = aoi_from_alcaldia(
        dsn=args.dsn,
        cve_ent=args.cve_ent,
        cve_mun=args.cve_mun,
        name=args.name,
        project_name=args.project_name,
    )
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


def _handle_cdmx_aoi_from_state(args: argparse.Namespace) -> int:
    payload = aoi_from_state(
        dsn=args.dsn,
        cve_ent=args.cve_ent,
        name=args.name,
        project_name=args.project_name,
    )
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


def _handle_indicators_seed_catalog(args: argparse.Namespace) -> int:
    return _print(seed_indicator_catalog(dsn=args.dsn))


def _handle_indicators_compute(args: argparse.Namespace) -> int:
    return _print(
        compute_indicator(
            dsn=args.dsn,
            slug=args.slug,
            aoi_id=args.aoi_id,
            scenario_id=args.scenario_id,
            code_version=args.code_version,
        )
    )


def _handle_evaluate_scenario(args: argparse.Namespace) -> int:
    mode = "detach" if args.detach else "sync" if args.sync else "embedded"
    return _print(
        evaluate_scenario_cli(
            dsn=args.dsn,
            changed_attributes=args.changed_attribute,
            edited_features=args.feature,
            scenario_id=args.scenario_id,
            aoi_id=args.aoi_id,
            code_version=args.code_version,
            mode=mode,
            timeout_s=args.timeout,
        )
    )


def _handle_indicators_rollup(args: argparse.Namespace) -> int:
    return _print(
        rollup_indicator(
            dsn=args.dsn,
            slug=args.slug,
            source_feature_type=args.source_type,
            target_feature_type=args.target_type,
            scenario_id=args.scenario_id,
            code_version=args.code_version,
        )
    )


def _handle_indicators_aoi_summary(args: argparse.Namespace) -> int:
    return _print(
        summarise_aoi_indicator(
            dsn=args.dsn,
            aoi_id=args.aoi_id,
            slug=args.slug,
            scenario_id=args.scenario_id,
        )
    )


def _handle_overlap_declare(args: argparse.Namespace) -> int:
    return _print(
        declare_overlap_pair(
            dsn=args.dsn,
            source_context=args.source_context,
            source_slug=args.source_slug,
            target_context=args.target_context,
            target_slug=args.target_slug,
        )
    )


def _handle_overlap_list(args: argparse.Namespace) -> int:
    return _print(list_overlap_pairs(dsn=args.dsn))


def _handle_overlap_compute_all(args: argparse.Namespace) -> int:
    return _print(compute_overlaps_all(dsn=args.dsn, aoi_id=args.aoi_id))


def _handle_raster_ingest(args: argparse.Namespace) -> int:
    return _print(
        ingest_raster(
            dsn=args.dsn,
            raw_path=args.raw_path,
            source_name=args.source_name,
            provider=args.provider,
            version=args.version,
            layer_slug=args.layer_slug,
            aggregation_kind=args.aggregation_kind,
            logical_name=args.logical_name,
            description=args.description,
            colormap_name=args.colormap_name,
            rescale=args.rescale,
            license=args.license,
            captured_at=args.captured_at,
            minzoom=args.minzoom,
            maxzoom=args.maxzoom,
            published=not args.unpublished,
            code_version=args.code_version,
            orchestrator_run_id=args.orchestrator_run_id,
        )
    )


def _handle_raster_fetch_worldpop(args: argparse.Namespace) -> int:
    return _print(
        fetch_worldpop_raster(
            country=args.country,
            year=args.year,
            output_path=args.output_path,
            cache_dir=args.cache_dir,
            dataset=args.dataset,
            aoi_id=args.aoi_id,
            dsn=args.dsn,
            force=args.force,
        )
    )


def _handle_raster_list(args: argparse.Namespace) -> int:
    return _print(list_raster_layers(dsn=args.dsn))


def _handle_raster_publish(args: argparse.Namespace) -> int:
    return _print(
        set_raster_published(
            dsn=args.dsn,
            layer_slug=args.layer_slug,
            published=not args.unpublish,
        )
    )


def _handle_raster_zonal_stats(args: argparse.Namespace) -> int:
    try:
        return _print(
            run_zonal_stats(
                dsn=args.dsn,
                source_id=args.source_id,
                feature_type_slug=args.feature_type,
                attribute_key_prefix=args.attribute_key_prefix,
                aoi_id=args.aoi_id,
                coverage_threshold=args.coverage_threshold,
                statistics=args.statistics,
                code_version=args.code_version,
                force=args.force,
                direct_dsn=args.direct_dsn,
            )
        )
    except PrefixCollisionError as exc:
        # Exit 2 (not 1) so operator scripts can distinguish "refused
        # by safety check" from "runtime/DB error". stderr so the
        # message doesn't pollute pipe-parsable stdout.
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _handle_derivation_add(args: argparse.Namespace) -> int:
    try:
        return _print(
            add_derivation(
                dsn=args.dsn,
                input_kind=args.input_kind,
                target_feature_type=args.target_feature_type,
                attribute_key_prefix=args.attribute_key_prefix,
                method=args.method,
                raster_layer_slug=args.raster_layer_slug,
                input_prefix=args.input_prefix,
                input_feature_type=args.input_feature_type,
                weight_basis=args.weight_basis,
                params_json=args.params,
                coverage_threshold=args.coverage_threshold,
                aoi_id=args.aoi_id,
                enabled=not args.disabled,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _handle_derivation_list(args: argparse.Namespace) -> int:
    try:
        return _print(
            list_derivations(
                dsn=args.dsn,
                raster_layer_slug=args.raster_layer_slug,
                include_disabled=args.include_disabled,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _handle_derivation_enable(args: argparse.Namespace) -> int:
    return _print(
        set_derivation_enabled(
            dsn=args.dsn,
            recipe_id=args.recipe_id,
            enabled=not args.disable,
        )
    )


def _handle_derivation_remove(args: argparse.Namespace) -> int:
    return _print(
        remove_derivation(
            dsn=args.dsn,
            recipe_id=args.recipe_id,
        )
    )


def _handle_derivation_run(args: argparse.Namespace) -> int:
    try:
        return _print(
            run_derivation(
                dsn=args.dsn,
                recipe_id=args.recipe_id,
                raster_layer_slug=args.raster_layer_slug,
                direct_dsn=args.direct_dsn,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
