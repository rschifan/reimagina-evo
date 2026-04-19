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
from .explore import (  # noqa: E402
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
    print_extract_sql,
    validate_mexico_city_buildings,
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
    bootstrap_cdmx,
    cdmx_status,
    fetch_cdmx_layer,
)
from .feat import seed_types  # noqa: E402
from .indicators import (  # noqa: E402
    compute_indicator,
    seed_indicator_catalog,
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
    explore_subparsers = explore_parser.add_subparsers(dest="explore_command", required=True)

    explore_layers = explore_subparsers.add_parser("layers", help="List the curated exploration catalog")
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

    overture_parser = subparsers.add_parser("overture", help="First Overture source operations")
    overture_subparsers = overture_parser.add_subparsers(dest="overture_command", required=True)

    overture_example = overture_subparsers.add_parser(
        "example",
        help="Show the pinned first-source example configuration",
    )
    overture_example.set_defaults(handler=_handle_overture_example)

    overture_extract_sql = overture_subparsers.add_parser(
        "print-extract-sql",
        help="Print the DuckDB SQL used to extract the Mexico City GeoParquet subset",
    )
    overture_extract_sql.set_defaults(handler=_handle_overture_print_extract_sql)

    overture_extract = overture_subparsers.add_parser(
        "extract",
        help="Extract the Mexico City buildings subset to local GeoParquet using DuckDB",
    )
    overture_extract.add_argument(
        "--output-path",
        default="data/overture_buildings_mexico_city.parquet",
    )
    overture_extract.set_defaults(handler=_handle_overture_extract)

    overture_load = overture_subparsers.add_parser(
        "load",
        help="Register, stage, and load the Mexico City buildings subset",
    )
    overture_load.add_argument("--dsn", default=DEFAULT_APP_DSN)
    overture_load.add_argument("--raw-path", required=True)
    overture_load.add_argument("--orchestrator-run-id")
    overture_load.add_argument("--code-version", default="dev")
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
    perf_seed.add_argument("--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke")
    perf_seed.add_argument("--feature-limit", type=int)
    perf_seed.set_defaults(handler=_handle_perf_seed_observations)

    perf_run = perf_subparsers.add_parser(
        "run",
        help="Run the named benchmark corpus and emit a structured JSON report",
    )
    perf_run.add_argument("--dsn", default=DEFAULT_APP_DSN)
    perf_run.add_argument("--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke")
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
    perf_explain.add_argument("--dataset-tier", choices=["smoke", "representative", "stress"], default="smoke")
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
    tiles_export.add_argument("--snapshot-base-url", default="http://localhost:3001")
    tiles_export.add_argument("--manifest-path")
    tiles_export.set_defaults(handler=_handle_tiles_export)

    tiles_sources = tiles_subparsers.add_parser(
        "sources",
        help="Resolve frontend-facing layer source configs from the manifest and snapshot registry",
    )
    tiles_sources.add_argument("--dsn", default=DEFAULT_APP_DSN)
    tiles_sources.add_argument("--live-tilejson-base-url", default="http://localhost:3000/get_tile")
    tiles_sources.add_argument("--snapshot-base-url", default="http://localhost:3001")
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
    cdmx_fetch.add_argument("--layer", required=True,
                            choices=("municipalities", "agebs", "blocks", "colonias"))
    cdmx_fetch.add_argument("--overwrite", action="store_true")
    cdmx_fetch.set_defaults(handler=_handle_cdmx_fetch)

    cdmx_bootstrap = cdmx_subparsers.add_parser(
        "bootstrap",
        help="Fetch + stage + load CDMX layers into curated_geo.feature",
    )
    cdmx_bootstrap.add_argument("--dsn", default=DEFAULT_APP_DSN)
    cdmx_bootstrap.add_argument("--layers",
        help="Comma-separated layer keys; default is municipalities,agebs,blocks,colonias")
    cdmx_bootstrap.add_argument("--from-path", action="append", default=[],
        metavar="LAYER=PATH",
        help="Use a local archive instead of downloading, e.g. blocks=/path/to/09_ciudaddemexico.zip. Repeatable.")
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
    cdmx_aoi.add_argument("--cve-mun", required=True,
                          help="INEGI municipality code, e.g. 015 for Cuauhtémoc")
    cdmx_aoi.add_argument("--name", required=True)
    cdmx_aoi.add_argument("--project-name", default="CDMX Realistic-Scale Validation")
    cdmx_aoi.set_defaults(handler=_handle_cdmx_aoi_from_alcaldia)

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

    overlap_parser = subparsers.add_parser(
        "overlap",
        help="Feature-to-feature overlap pair declaration and compute",
    )
    overlap_subparsers = overlap_parser.add_subparsers(dest="overlap_command", required=True)

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

    return parser


def _print(data: dict[str, Any]) -> int:
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
    return _print({"dsn": args.dsn, "checks": checks, "all_present": all(checks.values())})


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


def _handle_overture_print_extract_sql(_args: argparse.Namespace) -> int:
    return _print(print_extract_sql())


def _handle_overture_extract(args: argparse.Namespace) -> int:
    return _print(extract_mexico_city_buildings(output_path=args.output_path))


def _handle_overture_load(args: argparse.Namespace) -> int:
    return _print(
        load_mexico_city_buildings(
            dsn=args.dsn,
            raw_path=args.raw_path,
            orchestrator_run_id=args.orchestrator_run_id,
            code_version=args.code_version,
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


def _handle_cdmx_aoi_from_alcaldia(args: argparse.Namespace) -> int:
    payload = aoi_from_alcaldia(
        dsn=args.dsn,
        cve_mun=args.cve_mun,
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
