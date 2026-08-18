"""Developer CLI entrypoint for local bootstrap and first-source workflows."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import click
import typer

try:
    # typer 0.26 vendors its own click fork; its exceptions are NOT subclasses
    # of upstream click's. Fall back to upstream if a future typer drops the
    # vendored fork, so the CLI never crashes at import time.
    #
    # The two branches deliberately bind different classes to one name, which
    # is what the fallback is for, so the assignment is ignored rather than
    # unified — and the `click_type=click.Choice(...)` sites below are typer's
    # documented idiom, which its stubs carry no overload for. Both suppressions
    # are stub friction, not defects: all 48 commands answer `--help` and the
    # suite is green.
    from typer._click.exceptions import ClickException as _TyperClickException
except ImportError:  # pragma: no cover
    from click.exceptions import ClickException as _TyperClickException  # type: ignore[assignment]

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


def _print(data: dict[str, Any]) -> int:
    # Never echo a DSN password into stdout/logs.
    if isinstance(data.get("dsn"), str):
        data = {**data, "dsn": mask_dsn(data["dsn"])}
    print(as_json(data))
    return 0


def _parse_from_path_pairs(raw: list[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            raise SystemExit(f"--from-path expects LAYER=PATH, got {entry!r}")
        key, path = entry.split("=", 1)
        pairs[key.strip()] = path.strip()
    return pairs


app = typer.Typer(
    name="reimagina-cli",
    add_completion=False,
    pretty_exceptions_enable=False,
)


# ── top-level commands ───────────────────────────────────────────────────
@app.command("status", help="Show CLI scaffold status")
def status() -> int:
    return _print(
        {
            **create_cli(),
            "examples": cli_examples(),
        }
    )


@app.command(
    "bootstrap-db",
    help="Create local app/test DBs if needed and migrate both to head",
)
def bootstrap_db(
    app_dsn: str = typer.Option(DEFAULT_APP_DSN, "--app-dsn"),
    test_dsn: str = typer.Option(DEFAULT_TEST_DSN, "--test-dsn"),
    admin_dsn: str = typer.Option(DEFAULT_ADMIN_DSN, "--admin-dsn"),
) -> int:
    return _print(
        bootstrap_local_platform(
            app_dsn=app_dsn,
            test_dsn=test_dsn,
            admin_dsn=admin_dsn,
        )
    )


@app.command(
    "verify-schema",
    help="Check that the canonical first-layer schemas/tables/views exist",
)
def verify_schema(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    with connect(dsn) as conn:
        checks = verify_required_relations(conn)
    return _print({"dsn": dsn, "checks": checks, "all_present": all(checks.values())})


# ── explore ──────────────────────────────────────────────────────────────
explore_app = typer.Typer(
    name="explore",
    help="Read-only inspection commands for the engineer-facing explore schema",
)
app.add_typer(explore_app, name="explore")


@explore_app.command("layers", help="List the curated exploration catalog")
def explore_layers(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_layers(dsn=dsn))


@explore_app.command(
    "sources", help="List source registry rows joined with ingestion runs"
)
def explore_sources(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_sources(dsn=dsn))


@explore_app.command(
    "feature-stats",
    help="Show canonical feature counts by type, role, source, and dataset version",
)
def explore_feature_stats(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_feature_stats(dsn=dsn))


@explore_app.command(
    "serving-stats", help="Show high-level counts over serving_geo.feature_v"
)
def explore_serving_stats(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_serving_stats(dsn=dsn))


# ── overture ─────────────────────────────────────────────────────────────
overture_app = typer.Typer(name="overture", help="First Overture source operations")
app.add_typer(overture_app, name="overture")


@overture_app.command(
    "example", help="Show the pinned first-source example configuration"
)
def overture_example() -> int:
    return _print(mexico_city_buildings_example())


@overture_app.command(
    "extract",
    help="Extract an Overture buildings subset to local GeoParquet via DuckDB",
)
def overture_extract(
    output_path: str = typer.Option(
        "data/overture_buildings_mexico_city.parquet", "--output-path"
    ),
    release: str = typer.Option(
        OVERTURE_DEFAULT_RELEASE,
        "--release",
        help="Overture S3 release version (e.g. 2026-04-15.0)",
    ),
    bbox: str = typer.Option(
        ",".join(str(v) for v in MEXICO_CITY_BBOX),
        "--bbox",
        help="Clip box xmin,ymin,xmax,ymax in EPSG:4326 (default: Mexico City)",
    ),
) -> int:
    return _print(
        extract_mexico_city_buildings(
            output_path=output_path,
            release=release,
            bbox=parse_bbox(bbox),
        )
    )


@overture_app.command(
    "load", help="Register, stage, and load an Overture buildings extract"
)
def overture_load(
    raw_path: str = typer.Option(..., "--raw-path"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    orchestrator_run_id: Optional[str] = typer.Option(None, "--orchestrator-run-id"),
    code_version: str = typer.Option("dev", "--code-version"),
    release: str = typer.Option(
        OVERTURE_DEFAULT_RELEASE,
        "--release",
        help="Overture S3 release version this parquet was extracted from "
        "(stored on data_source_catalog.version + feature.dataset_version)",
    ),
    source_name: str = typer.Option(
        OVERTURE_BUILDINGS_SOURCE_NAME,
        "--source-name",
        help="data_source_catalog.name. Use a distinct value per AOI to "
        "avoid collapsing multiple bbox extracts onto the same source row",
    ),
    aoi_id: Optional[str] = typer.Option(
        None,
        "--aoi-id",
        help="Optional AOI uuid. When supplied, freshly-loaded buildings "
        "outside the AOI polygon are deleted post-load — required when "
        "the bbox is wider than the actual AOI",
    ),
    recompute: bool = typer.Option(
        False,
        "--recompute",
        help="After a successful load, submit an indicator run for the "
        "attributes it invalidated. Off by default: a city-scale load can "
        "invalidate every block-grain indicator, and those units are budgeted "
        "in minutes",
    ),
    recompute_attributes: list[str] = typer.Option(
        ["geometry"],
        "--recompute-attributes",
        metavar="ATTR",
        help="Attribute names the load changed, matched against each "
        "indicator's declared `reads` (default: geometry). Only meaningful "
        "with --recompute",
    ),
) -> int:
    return _print(
        load_mexico_city_buildings(
            dsn=dsn,
            raw_path=raw_path,
            orchestrator_run_id=orchestrator_run_id,
            code_version=code_version,
            release=release,
            source_name=source_name,
            aoi_id=aoi_id,
            recompute=recompute,
            recompute_attributes=tuple(recompute_attributes),
        )
    )


@overture_app.command(
    "validate",
    help="Validate that loaded buildings are visible through serving_geo",
)
def overture_validate(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(validate_mexico_city_buildings(dsn=dsn))


@explore_app.command(
    "executor-health",
    help="Show DBOS executor liveness from explore.ops_executor_health_v",
)
def explore_executor_health(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_executor_health(dsn=dsn))


@app.command(
    "evaluate-scenario",
    help=(
        "Selectively recompute indicators for a scenario edit "
        "(impact engine → coordinator)."
    ),
)
def evaluate_scenario(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    changed_attribute: list[str] = typer.Option(
        [],
        "--changed-attribute",
        metavar="ATTR",
        help="Attribute key the scenario changed (repeatable); seeds the dirty-set.",
    ),
    feature: list[str] = typer.Option(
        [],
        "--feature",
        metavar="FEATURE_ID",
        help="Feature id the change applies to (repeatable).",
    ),
    aoi_id: Optional[str] = typer.Option(None, "--aoi-id"),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    code_version: str = typer.Option("dev", "--code-version"),
    timeout: float = typer.Option(
        600.0,
        "--timeout",
        help="Seconds to wait for the run to finish (default 600; ignored for "
        "--detach / --sync). Unit concurrency is a property of the ops-plane "
        "queue (REIMAGINA_UNIT_CONCURRENCY), not of this command.",
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Enqueue and return immediately for a standalone "
        "`python -m apps.worker` fleet to drain (fire-and-forget; needs "
        "DATABASE_URL_DIRECT).",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Run the in-process orchestrator (no queue, no worker).",
    ),
) -> int:
    # argparse enforced this with add_mutually_exclusive_group; typer has no
    # equivalent, so the check is explicit. Dropping it would let --detach
    # --sync through and silently pick one.
    if detach and sync:
        raise typer.BadParameter("--detach and --sync are mutually exclusive")
    mode = "detach" if detach else "sync" if sync else "embedded"
    return _print(
        evaluate_scenario_cli(
            dsn=dsn,
            changed_attributes=changed_attribute,
            edited_features=feature,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
            mode=mode,
            timeout_s=timeout,
        )
    )


# ── perf ─────────────────────────────────────────────────────────────────
perf_app = typer.Typer(
    name="perf",
    help="Performance benchmark and observability commands for Postgres/PostGIS",
)
app.add_typer(perf_app, name="perf")


@perf_app.command(
    "seed-observations",
    help="Seed deterministic observations and AOIs for benchmark tiers",
)
def perf_seed_observations(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    dataset_tier: str = typer.Option(  # type: ignore[call-overload]
        "smoke",
        "--dataset-tier",
        click_type=click.Choice(["smoke", "representative", "stress"]),
    ),
    feature_limit: Optional[int] = typer.Option(None, "--feature-limit"),
) -> int:
    return _print(
        seed_observations(
            dsn=dsn,
            dataset_tier=dataset_tier,
            feature_limit=feature_limit,
        )
    )


@perf_app.command(
    "run", help="Run the named benchmark corpus and emit a structured JSON report"
)
def perf_run(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    dataset_tier: str = typer.Option(  # type: ignore[call-overload]
        "smoke",
        "--dataset-tier",
        click_type=click.Choice(["smoke", "representative", "stress"]),
    ),
    benchmark: Optional[list[str]] = typer.Option(None, "--benchmark"),
    iterations: Optional[int] = typer.Option(None, "--iterations"),
    output_path: Optional[str] = typer.Option(None, "--output-path"),
) -> int:
    return _print(
        run_perf_benchmarks(
            dsn=dsn,
            dataset_tier=dataset_tier,
            benchmark_names=benchmark,
            iterations=iterations,
            output_path=output_path,
        )
    )


@perf_app.command(
    "explain", help="Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for one benchmark"
)
def perf_explain(
    benchmark_name: str = typer.Argument(...),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    dataset_tier: str = typer.Option(  # type: ignore[call-overload]
        "smoke",
        "--dataset-tier",
        click_type=click.Choice(["smoke", "representative", "stress"]),
    ),
) -> int:
    return _print(
        explain_query(
            dsn=dsn,
            benchmark_name=benchmark_name,
            dataset_tier=dataset_tier,
        )
    )


@perf_app.command(
    "report",
    help="Show a live Postgres observability report for performance monitoring",
)
def perf_report(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    limit: int = typer.Option(10, "--limit"),
    format: str = typer.Option("json", "--format", click_type=click.Choice(["json"])),  # type: ignore[call-overload]
) -> int:
    return _print(
        performance_report(
            dsn=dsn,
            limit=limit,
        )
    )


@perf_app.command("compare", help="Compare two benchmark result JSON files")
def perf_compare(
    baseline_path: str = typer.Argument(...),
    candidate_path: str = typer.Argument(...),
) -> int:
    return _print(
        compare_perf_reports(
            baseline_path=baseline_path,
            candidate_path=candidate_path,
        )
    )


# ── tiles ────────────────────────────────────────────────────────────────
tiles_app = typer.Typer(
    name="tiles",
    help="Hybrid live-MVT and PMTiles snapshot commands",
)
app.add_typer(tiles_app, name="tiles")


@tiles_app.command(
    "manifest",
    help="Show the infrastructure-owned layer manifest for live and snapshot tile sources",
)
def tiles_manifest(
    manifest_path: Optional[str] = typer.Option(None, "--manifest-path"),
) -> int:
    return _print(tile_manifest(manifest_path=manifest_path))


@tiles_app.command(
    "export", help="Build a PMTiles snapshot for a configured stable layer"
)
def tiles_export(
    layer_id: str = typer.Argument(...),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    output_dir: str = typer.Option("data/tiles/pmtiles", "--output-dir"),
    snapshot_base_url: str = typer.Option(
        tile_snapshot_base_url(), "--snapshot-base-url"
    ),
    manifest_path: Optional[str] = typer.Option(None, "--manifest-path"),
) -> int:
    return _print(
        export_tile_snapshot(
            dsn=dsn,
            layer_id=layer_id,
            output_dir=output_dir,
            snapshot_base_url=snapshot_base_url,
            manifest_path=manifest_path,
        )
    )


@tiles_app.command(
    "sources",
    help="Resolve frontend-facing layer source configs from the manifest and snapshot registry",
)
def tiles_sources(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    live_tilejson_base_url: str = typer.Option(
        tile_live_base_url(), "--live-tilejson-base-url"
    ),
    snapshot_base_url: str = typer.Option(
        tile_snapshot_base_url(), "--snapshot-base-url"
    ),
    manifest_path: Optional[str] = typer.Option(None, "--manifest-path"),
) -> int:
    return _print(
        list_tile_sources(
            dsn=dsn,
            live_tilejson_base_url=live_tilejson_base_url,
            snapshot_base_url=snapshot_base_url,
            manifest_path=manifest_path,
        )
    )


@tiles_app.command("snapshots", help="List the PMTiles snapshot registry")
def tiles_snapshots(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    layer_id: Optional[str] = typer.Option(None, "--layer-id"),
) -> int:
    return _print(
        list_tile_snapshots(
            dsn=dsn,
            layer_id=layer_id,
        )
    )


# ── feat ─────────────────────────────────────────────────────────────────
feat_app = typer.Typer(name="feat", help="Canonical feature_type_catalog operations")
app.add_typer(feat_app, name="feat")


@feat_app.command("seed-types", help="Seed the MEX + global feature types (idempotent)")
def feat_seed_types(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(seed_types(dsn=dsn))


# ── cdmx ─────────────────────────────────────────────────────────────────
cdmx_app = typer.Typer(
    name="cdmx",
    help="CDMX canonical-geometry bootstrap (INEGI + datos.cdmx.gob.mx)",
)
app.add_typer(cdmx_app, name="cdmx")


@cdmx_app.command("fetch", help="Download (or reuse cached) archive for one CDMX layer")
def cdmx_fetch(
    layer: str = typer.Option(  # type: ignore[call-overload]
        ...,
        "--layer",
        click_type=click.Choice(["municipalities", "agebs", "blocks", "colonias"]),
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> int:
    payload = fetch_cdmx_layer(layer=layer, overwrite=overwrite)
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


@cdmx_app.command(
    "bootstrap", help="Fetch + stage + load CDMX layers into curated_geo.feature"
)
def cdmx_bootstrap(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    layers: Optional[str] = typer.Option(
        None,
        "--layers",
        help="Comma-separated layer keys; default is municipalities,agebs,blocks,colonias",
    ),
    from_path: list[str] = typer.Option(
        [],
        "--from-path",
        metavar="LAYER=PATH",
        help="Use a local archive instead of downloading, e.g. blocks=/path/to/09_ciudaddemexico.zip. Repeatable.",
    ),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    layers_list = [k.strip() for k in layers.split(",")] if layers else None
    from_paths = _parse_from_path_pairs(from_path)
    return _print(
        bootstrap_cdmx(
            dsn=dsn,
            layers=layers_list,
            from_paths=from_paths,
            code_version=code_version,
        )
    )


@cdmx_app.command("status", help="Row counts per MEX feature type")
def cdmx_status_cmd(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(cdmx_status(dsn=dsn))


@cdmx_app.command(
    "aoi-from-alcaldia",
    help="Create an AOI whose geometry is the matching MEX:municipality polygon",
)
def cdmx_aoi_from_alcaldia(
    cve_mun: str = typer.Option(
        ...,
        "--cve-mun",
        help="INEGI municipality code, e.g. 015 for Cuauhtémoc, 002 for Mexicali",
    ),
    name: str = typer.Option(..., "--name"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    cve_ent: str = typer.Option(
        "09",
        "--cve-ent",
        help="INEGI state code; default 09 (CDMX). Use 02 for Mexicali.",
    ),
    project_name: str = typer.Option(
        "CDMX Realistic-Scale Validation", "--project-name"
    ),
) -> int:
    payload = aoi_from_alcaldia(
        dsn=dsn,
        cve_ent=cve_ent,
        cve_mun=cve_mun,
        name=name,
        project_name=project_name,
    )
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


@cdmx_app.command(
    "aoi-from-state",
    help=(
        "Create an AOI whose geometry is the entire entidad (state) "
        "polygon — e.g. 'all of CDMX' as one AOI."
    ),
)
def cdmx_aoi_from_state(
    cve_ent: str = typer.Option(
        ...,
        "--cve-ent",
        help="INEGI state code, e.g. 09 for CDMX, 02 for Baja California.",
    ),
    name: str = typer.Option(..., "--name"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    project_name: str = typer.Option(
        "CDMX Realistic-Scale Validation", "--project-name"
    ),
) -> int:
    payload = aoi_from_state(
        dsn=dsn,
        cve_ent=cve_ent,
        name=name,
        project_name=project_name,
    )
    _print(payload)
    return 0 if payload.get("status") == "ok" else 2


# ── inegi-marco ──────────────────────────────────────────────────────────
inegi_marco_app = typer.Typer(
    name="inegi-marco",
    help="INEGI Marco Geoestadístico 2024 ingest from the data-lake mirror",
)
app.add_typer(inegi_marco_app, name="inegi-marco")


@inegi_marco_app.command(
    "bootstrap",
    help="Stage + load INEGI marco layers (filtered by CVE_ENT) into curated_geo.feature",
)
def inegi_marco_bootstrap(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    layers: Optional[str] = typer.Option(
        None,
        "--layers",
        help=(
            "Comma-separated layer keys. Default: all four "
            "(areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas)."
        ),
    ),
    cve_ent: list[str] = typer.Option(
        [],
        "--cve-ent",
        metavar="CVE_ENT",
        help=(
            "INEGI entidad code (e.g. 02 for Baja California, 09 for CDMX). "
            "Repeatable. If omitted, ingests ALL of Mexico — usually not what you want."
        ),
    ),
    source_root: Optional[str] = typer.Option(
        None,
        "--source-root",
        help=(
            "Override the source directory (defaults to "
            "data/sources/azure/inegi/marco_geostadistico/2024)."
        ),
    ),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    layers_list = [k.strip() for k in layers.split(",")] if layers else None
    cve_ent_filter = list(cve_ent) if cve_ent else None
    return _print(
        bootstrap_inegi_marco(
            dsn=dsn,
            layers=layers_list,
            cve_ent_filter=cve_ent_filter,
            source_root=source_root,
            code_version=code_version,
        )
    )


# ── inegi-censo ──────────────────────────────────────────────────────────
inegi_censo_app = typer.Typer(
    name="inegi-censo",
    help="INEGI 2020 census ingest (manzana-level → curated_geo.observation)",
)
app.add_typer(inegi_censo_app, name="inegi-censo")


@inegi_censo_app.command(
    "bootstrap",
    help="Read filtered 2020 census parquet, join to features, write observations",
)
def inegi_censo_bootstrap(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    cve_ent: list[str] = typer.Option(
        [],
        "--cve-ent",
        metavar="CVE_ENT",
        help=(
            "INEGI entidad code, e.g. 02 or 9 (auto-normalised). "
            "Repeatable. If omitted, ingests ALL of Mexico — usually "
            "not what you want."
        ),
    ),
    columns: Optional[str] = typer.Option(
        None,
        "--columns",
        help=(
            "Comma-separated census column names. Default: headline subset "
            "(pobtot, pobmas, pobfem, p_15ymas, p_18ymas, vivtot)."
        ),
    ),
    source_root: Optional[str] = typer.Option(
        None,
        "--source-root",
        help=(
            "Override the source directory (defaults to "
            "data/sources/azure/inegi/censo/manzanas_2020.parquet)."
        ),
    ),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    columns_list = [c.strip() for c in columns.split(",")] if columns else None
    cve_ent_filter = list(cve_ent) if cve_ent else None
    return _print(
        bootstrap_inegi_censo(
            dsn=dsn,
            cve_ent_filter=cve_ent_filter,
            columns=columns_list,
            source_root=source_root,
            code_version=code_version,
        )
    )


# ── data ─────────────────────────────────────────────────────────────────
data_app = typer.Typer(
    name="data",
    help="Cross-source operations (smoke tests, manifests)",
)
app.add_typer(data_app, name="data")


@data_app.command(
    "verify-sources",
    help="Check that every upstream source listed in the bootstrap "
    "runbook is reachable before triggering a long ingest",
)
def data_verify_sources(
    release: str = typer.Option(
        OVERTURE_DEFAULT_RELEASE,
        "--release",
        help="Overture release to probe on S3",
    ),
    worldpop_country: str = typer.Option(
        "MEX",
        "--worldpop-country",
        help="ISO3 country code used to probe WorldPop's REST catalogue. "
        "Default MEX so the smoke test mirrors the runbook.",
    ),
) -> int:
    result = verify_sources(
        release=release,
        worldpop_country=worldpop_country,
    )
    _print(result)
    return 0 if result["all_ok"] else 1


# ── aoi ──────────────────────────────────────────────────────────────────
aoi_app = typer.Typer(
    name="aoi",
    help="Area-of-interest lifecycle: list, delete (creators live under cdmx)",
)
app.add_typer(aoi_app, name="aoi")


@aoi_app.command("list", help="List every AOI with bbox + feature count")
def aoi_list(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_aois(dsn=dsn))


@aoi_app.command(
    "delete",
    help="Delete an AOI by uuid. CASCADE removes aoi_feature rows; "
    "features themselves are preserved",
)
def aoi_delete(
    aoi_id: str = typer.Option(..., "--aoi-id"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
) -> int:
    return _print(delete_aoi(dsn=dsn, aoi_id=aoi_id))


@aoi_app.command(
    "compute-features",
    help="(Re)populate aoi_feature for one or all AOIs. Idempotent.",
)
def aoi_compute_features(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    aoi_id: Optional[str] = typer.Option(
        None,
        "--aoi-id",
        help="Optional. Limit to a single AOI uuid; default runs every AOI.",
    ),
) -> int:
    return _print(compute_aoi_features_cli(dsn=dsn, aoi_id=aoi_id))


# ── points ───────────────────────────────────────────────────────────────
points_app = typer.Typer(
    name="points",
    help="Per-pilot-city amenity / POI ingest from the data-lake mirror",
)
app.add_typer(points_app, name="points")


@points_app.command(
    "bootstrap",
    help="Stage + load amenity GeoParquet layers into curated_geo.feature",
)
def points_bootstrap(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    layers: Optional[str] = typer.Option(
        None,
        "--layers",
        help=(
            "Comma-separated layer keys. Default: all "
            "(tlalpan_amenities,mexicali_amenities)."
        ),
    ),
    source_root: Optional[str] = typer.Option(
        None,
        "--source-root",
        help="Override the source directory (defaults to data/sources/azure).",
    ),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    layers_list = [k.strip() for k in layers.split(",")] if layers else None
    return _print(
        bootstrap_points(
            dsn=dsn,
            layers=layers_list,
            source_root=source_root,
            code_version=code_version,
        )
    )


# ── indicators ───────────────────────────────────────────────────────────
indicators_app = typer.Typer(
    name="indicators",
    help="Indicator catalog management and compute dispatch",
)
app.add_typer(indicators_app, name="indicators")


@indicators_app.command(
    "seed-catalog",
    help="Register every shipped indicator in curated_geo.indicator_catalog",
)
def indicators_seed_catalog(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(seed_indicator_catalog(dsn=dsn))


@indicators_app.command(
    "compute", help="Run the compute worker for a single indicator slug"
)
def indicators_compute(
    slug: str = typer.Option(..., "--slug"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    aoi_id: Optional[str] = typer.Option(None, "--aoi-id"),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    return _print(
        compute_indicator(
            dsn=dsn,
            slug=slug,
            aoi_id=aoi_id,
            scenario_id=scenario_id,
            code_version=code_version,
        )
    )


@indicators_app.command(
    "rollup",
    help="Roll an indicator up from a source grain to a coarser target grain",
)
def indicators_rollup(
    slug: str = typer.Option(..., "--slug"),
    source_type: str = typer.Option(..., "--source-type"),
    target_type: str = typer.Option(..., "--target-type"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    code_version: str = typer.Option("dev", "--code-version"),
) -> int:
    return _print(
        rollup_indicator(
            dsn=dsn,
            slug=slug,
            source_feature_type=source_type,
            target_feature_type=target_type,
            scenario_id=scenario_id,
            code_version=code_version,
        )
    )


@indicators_app.command(
    "aoi-summary",
    help="Compute one aoi_indicator_summary row for (aoi, indicator, scenario)",
)
def indicators_aoi_summary(
    aoi_id: str = typer.Option(..., "--aoi-id"),
    slug: str = typer.Option(..., "--slug"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
) -> int:
    return _print(
        summarise_aoi_indicator(
            dsn=dsn,
            aoi_id=aoi_id,
            slug=slug,
            scenario_id=scenario_id,
        )
    )


# ── overlap ──────────────────────────────────────────────────────────────
overlap_app = typer.Typer(
    name="overlap",
    help="Feature-to-feature overlap pair declaration and compute",
)
app.add_typer(overlap_app, name="overlap")


@overlap_app.command(
    "declare-pair",
    help="Register a source→target type pair in feature_overlap_type_pair",
)
def overlap_declare(
    source_context: str = typer.Option(..., "--source-context"),
    source_slug: str = typer.Option(..., "--source-slug"),
    target_context: str = typer.Option(..., "--target-context"),
    target_slug: str = typer.Option(..., "--target-slug"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
) -> int:
    return _print(
        declare_overlap_pair(
            dsn=dsn,
            source_context=source_context,
            source_slug=source_slug,
            target_context=target_context,
            target_slug=target_slug,
        )
    )


@overlap_app.command("list-pairs", help="List declared type-pair rows")
def overlap_list(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_overlap_pairs(dsn=dsn))


@overlap_app.command(
    "compute-all",
    help="Compute overlaps for every declared pair (AOI-scoped recommended)",
)
def overlap_compute_all(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    aoi_id: Optional[str] = typer.Option(None, "--aoi-id"),
) -> int:
    return _print(compute_overlaps_all(dsn=dsn, aoi_id=aoi_id))


# ── raster ───────────────────────────────────────────────────────────────
raster_app = typer.Typer(
    name="raster",
    help="Single-band raster ingest + registry operations",
)
app.add_typer(raster_app, name="raster")


@raster_app.command(
    "ingest",
    help="Register + stage (bronze → silver COG) a single-band raster, "
    "then insert a raster_layer row so /tile-sources picks it up",
)
def raster_ingest(
    raw_path: str = typer.Option(
        ...,
        "--raw-path",
        help="Path to the raw single-band GeoTIFF / COG / NetCDF. "
        "Multi-band containers (HDF, multi-band NetCDF) are out of v1 "
        "scope — pre-extract one band with `gdal_translate -b <N>`.",
    ),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    source_name: str = typer.Option(
        ..., "--source-name", help='e.g. "MODIS LST 2026-01-15 day"'
    ),
    provider: str = typer.Option(..., "--provider", help='e.g. "NASA LAADS"'),
    version: str = typer.Option(
        ...,
        "--version",
        help="Data version (e.g. acquisition date); rides along on source + asset rows",
    ),
    layer_slug: str = typer.Option(
        ...,
        "--layer-slug",
        help="Stable slug for the raster_layer row (e.g. lst_day_2026_01_15)",
    ),
    aggregation_kind: str = typer.Option(  # type: ignore[call-overload]
        "intensive",
        "--aggregation-kind",
        click_type=click.Choice(["intensive", "extensive"]),
        help="Drives default zonal-stats; use 'extensive' for count/sum-per-pixel rasters",
    ),
    logical_name: Optional[str] = typer.Option(None, "--logical-name"),
    description: Optional[str] = typer.Option(None, "--description"),
    colormap_name: Optional[str] = typer.Option(
        None, "--colormap-name", help='e.g. "inferno"'
    ),
    rescale: Optional[str] = typer.Option(
        None, "--rescale", help='e.g. "270,320" (min,max)'
    ),
    license: Optional[str] = typer.Option(None, "--license"),
    captured_at: Optional[str] = typer.Option(
        None,
        "--captured-at",
        help="ISO-8601 timestamp; stored on file_asset.captured_at",
    ),
    minzoom: int = typer.Option(5, "--minzoom"),
    maxzoom: int = typer.Option(14, "--maxzoom"),
    unpublished: bool = typer.Option(
        False,
        "--unpublished",
        help="Register the raster but hide it from /tile-sources until "
        "`raster publish --slug <slug>` flips it on",
    ),
    code_version: str = typer.Option("dev", "--code-version"),
    orchestrator_run_id: Optional[str] = typer.Option(None, "--orchestrator-run-id"),
) -> int:
    return _print(
        ingest_raster(
            dsn=dsn,
            raw_path=raw_path,
            source_name=source_name,
            provider=provider,
            version=version,
            layer_slug=layer_slug,
            aggregation_kind=aggregation_kind,
            logical_name=logical_name,
            description=description,
            colormap_name=colormap_name,
            rescale=rescale,
            license=license,
            captured_at=captured_at,
            minzoom=minzoom,
            maxzoom=maxzoom,
            published=not unpublished,
            code_version=code_version,
            orchestrator_run_id=orchestrator_run_id,
        )
    )


@raster_app.command(
    "fetch-worldpop",
    help="Download a WorldPop country raster from the public REST API "
    "(cached) and optionally clip it to an AOI polygon. Output is "
    "a local GeoTIFF ready for `raster ingest --raw-path`.",
)
def raster_fetch_worldpop(
    country: str = typer.Option(
        ...,
        "--country",
        help="ISO3 country code, e.g. MEX. Used to query "
        "https://www.worldpop.org/rest/data/pop/<dataset>?iso3=<C>",
    ),
    year: int = typer.Option(
        ...,
        "--year",
        help="Population year (e.g. 2020). Must match a popyear row in "
        "the WorldPop catalogue for the chosen country + dataset.",
    ),
    output_path: str = typer.Option(
        ...,
        "--output-path",
        help="Where to write the GeoTIFF the operator passes to "
        "`raster ingest --raw-path` (e.g. data/worldpop_cdmx.tif)",
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Where to cache the country-scale GeoTIFF so re-running "
        "against another AOI doesn't re-download. Defaults to "
        "data/cache/worldpop/.",
    ),
    dataset: str = typer.Option(
        "wpgp",
        "--dataset",
        help="WorldPop dataset slug (default 'wpgp' = per-pixel population).",
    ),
    aoi_id: Optional[str] = typer.Option(
        None,
        "--aoi-id",
        help="When set, clip the country raster to this AOI's polygon "
        "(read from curated_geo.area_of_interest). Without --aoi-id "
        "the country raster is copied through verbatim — fine for "
        "small countries but ~1 GB for Mexico.",
    ),
    dsn: str = typer.Option(
        DEFAULT_APP_DSN,
        "--dsn",
        help="DSN used to read the AOI polygon when --aoi-id is given.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-download the country raster even if cached. Use only "
        "when a previous fetch finished partial / corrupt.",
    ),
) -> int:
    return _print(
        fetch_worldpop_raster(
            country=country,
            year=year,
            output_path=output_path,
            cache_dir=cache_dir,
            dataset=dataset,
            aoi_id=aoi_id,
            dsn=dsn,
            force=force,
        )
    )


@raster_app.command(
    "list",
    help="List every registered raster layer (reads explore.raster_layer_v)",
)
def raster_list(dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn")) -> int:
    return _print(list_raster_layers(dsn=dsn))


@raster_app.command(
    "publish", help="Flip the published flag on a raster_layer row by layer_slug"
)
def raster_publish(
    layer_slug: str = typer.Option(..., "--layer-slug"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    unpublish: bool = typer.Option(
        False,
        "--unpublish",
        help="Set published=false instead of true (default: publish)",
    ),
) -> int:
    return _print(
        set_raster_published(
            dsn=dsn,
            layer_slug=layer_slug,
            published=not unpublish,
        )
    )


@raster_app.command(
    "zonal-stats",
    help="Compute zonal stats on a raster for every feature of a type, "
    "writing one observation row per (feature, statistic)",
)
def raster_zonal_stats(
    source_id: str = typer.Option(
        ...,
        "--source-id",
        help="UUID of the raster source (from `raster ingest` output or "
        "curated_geo.data_source_catalog)",
    ),
    feature_type: str = typer.Option(
        ...,
        "--feature-type",
        help="Feature type slug (e.g. 'block', 'ageb') — must be a MEX "
        "type registered in curated_geo.feature_type_catalog",
    ),
    attribute_key_prefix: str = typer.Option(
        ...,
        "--attribute-key-prefix",
        help="Prefix for the observation attribute_key (e.g. 'lst_day'); "
        "stats land as '<prefix>_<stat>' (e.g. 'lst_day_mean')",
    ),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    aoi_id: Optional[str] = typer.Option(
        None,
        "--aoi-id",
        help="Restrict to features intersecting this AOI; omit for all features of the type",
    ),
    coverage_threshold: float = typer.Option(
        0.0,
        "--coverage-threshold",
        help="Skip features whose fractional valid-pixel count is below this. "
        "Default 0.0 — write a row for every polygon that overlaps any "
        "non-nodata cell. Under exactextract's area-weighting, sub-pixel "
        "polygons (manzanas < 1 ha) get fractional coverage 0..1; values "
        ">0 still produce correct area-weighted sums. Raise (e.g. 0.5) "
        "only when explicitly excluding partially-covered polygons.",
    ),
    statistics: Optional[str] = typer.Option(
        None,
        "--statistics",
        help="Comma-separated stat names (e.g. 'mean,median,p05,p95'); "
        "omit to use the aggregation_kind default from raster_layer",
    ),
    code_version: str = typer.Option("dev", "--code-version"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Override the prefix-collision check; use only when you have "
        "verified that co-ownership of the prefix is intentional",
    ),
    direct_dsn: Optional[str] = typer.Option(
        None,
        "--direct-dsn",
        help="Postgres-only: direct-port DSN for the advisory-lock "
        "transaction. Defaults to $DATABASE_URL_DIRECT.",
    ),
) -> int:
    try:
        return _print(
            run_zonal_stats(
                dsn=dsn,
                source_id=source_id,
                feature_type_slug=feature_type,
                attribute_key_prefix=attribute_key_prefix,
                aoi_id=aoi_id,
                coverage_threshold=coverage_threshold,
                statistics=statistics,
                code_version=code_version,
                force=force,
                direct_dsn=direct_dsn,
            )
        )
    except PrefixCollisionError as exc:
        # Exit 2 (not 1) so operator scripts can distinguish "refused
        # by safety check" from "runtime/DB error". stderr so the
        # message doesn't pollute pipe-parsable stdout.
        print(f"error: {exc}", file=sys.stderr)
        return 2


# ── raster derivation (aggregation recipes, r020) ────────────────────────
derivation_app = typer.Typer(
    name="derivation",
    help="Declarative aggregation recipes: raster→observation and "
    "observation→observation derivations per target feature grain",
)
raster_app.add_typer(derivation_app, name="derivation")


@derivation_app.command(
    "add",
    help="Register a recipe (raster or observation input). Use 'run' "
    "to materialise the observations it produces.",
)
def derivation_add(
    input_kind: str = typer.Option(  # type: ignore[call-overload]
        ...,
        "--input-kind",
        click_type=click.Choice(["raster", "observation"]),
    ),
    target_feature_type: str = typer.Option(
        ...,
        "--target-feature-type",
        help="Feature type slug of the output grain (e.g. 'municipality')",
    ),
    attribute_key_prefix: str = typer.Option(
        ...,
        "--attribute-key-prefix",
        help="Output prefix — one observation per (feature, <prefix>_<suffix>)",
    ),
    method: str = typer.Option(  # type: ignore[call-overload]
        ...,
        "--method",
        click_type=click.Choice(["mean", "weighted_mean", "sum", "min", "max"]),
    ),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    raster_layer_slug: Optional[str] = typer.Option(
        None, "--raster-layer-slug", help="Required if --input-kind=raster"
    ),
    input_prefix: Optional[str] = typer.Option(
        None,
        "--input-prefix",
        help="Required if --input-kind=observation (the attribute_key_prefix on the source observations)",
    ),
    input_feature_type: Optional[str] = typer.Option(
        None,
        "--input-feature-type",
        help="Required if --input-kind=observation (grain of the source observations)",
    ),
    weight_basis: Optional[str] = typer.Option(  # type: ignore[call-overload]
        None,
        "--weight-basis",
        click_type=click.Choice(["area", "length", "presence"]),
        help="Required with --method=weighted_mean for observation inputs",
    ),
    params: Optional[str] = typer.Option(
        None,
        "--params",
        help='JSON blob of method-specific params (e.g. \'{"stats":["sum","count"]}\' '
        "for raster inputs that want multiple rasterstats primitives)",
    ),
    coverage_threshold: float = typer.Option(0.5, "--coverage-threshold"),
    aoi_id: Optional[str] = typer.Option(
        None, "--aoi-id", help="Restrict recipe execution to features inside this AOI"
    ),
    disabled: bool = typer.Option(
        False,
        "--disabled",
        help="Register the recipe in a disabled state; enable later with "
        "`derivation enable`",
    ),
) -> int:
    try:
        return _print(
            add_derivation(
                dsn=dsn,
                input_kind=input_kind,
                target_feature_type=target_feature_type,
                attribute_key_prefix=attribute_key_prefix,
                method=method,
                raster_layer_slug=raster_layer_slug,
                input_prefix=input_prefix,
                input_feature_type=input_feature_type,
                weight_basis=weight_basis,
                params_json=params,
                coverage_threshold=coverage_threshold,
                aoi_id=aoi_id,
                enabled=not disabled,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


@derivation_app.command(
    "list", help="Show recipes (reads serving_geo.aggregation_recipe_v)"
)
def derivation_list(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    raster_layer_slug: Optional[str] = typer.Option(
        None,
        "--raster-layer-slug",
        help="Filter to recipes whose input is this raster layer",
    ),
    include_disabled: bool = typer.Option(False, "--include-disabled"),
) -> int:
    try:
        return _print(
            list_derivations(
                dsn=dsn,
                raster_layer_slug=raster_layer_slug,
                include_disabled=include_disabled,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


@derivation_app.command(
    "enable", help="Flip enabled=true on a recipe (opposite: `derivation disable`)"
)
def derivation_enable(
    recipe_id: str = typer.Option(..., "--recipe-id"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    disable: bool = typer.Option(
        False, "--disable", help="Set enabled=false instead of true"
    ),
) -> int:
    return _print(
        set_derivation_enabled(
            dsn=dsn,
            recipe_id=recipe_id,
            enabled=not disable,
        )
    )


@derivation_app.command(
    "remove",
    help="Hard-delete a recipe. Existing observations stay put; "
    "retire them separately if wanted.",
)
def derivation_remove(
    recipe_id: str = typer.Option(..., "--recipe-id"),
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
) -> int:
    return _print(
        remove_derivation(
            dsn=dsn,
            recipe_id=recipe_id,
        )
    )


@derivation_app.command(
    "run",
    help="Materialise one recipe by id, OR every enabled recipe for a raster layer",
)
def derivation_run(
    dsn: str = typer.Option(DEFAULT_APP_DSN, "--dsn"),
    recipe_id: Optional[str] = typer.Option(
        None, "--recipe-id", help="UUID of a single recipe to run"
    ),
    raster_layer_slug: Optional[str] = typer.Option(
        None,
        "--raster-layer-slug",
        help="Slug of a raster layer; runs every enabled recipe whose "
        "input is that layer",
    ),
    direct_dsn: Optional[str] = typer.Option(
        None,
        "--direct-dsn",
        help="Postgres-only: direct-port DSN used by the critical section's "
        "dedicated connection. Defaults to $DATABASE_URL_DIRECT.",
    ),
) -> int:
    try:
        return _print(
            run_derivation(
                dsn=dsn,
                recipe_id=recipe_id,
                raster_layer_slug=raster_layer_slug,
                direct_dsn=direct_dsn,
            )
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and RETURN the process exit code (never sys.exit).

    typer/click is invoked with ``standalone_mode=False`` so command bodies'
    return values propagate as the result instead of being thrown away. Each
    command returns its int exit code (``_print`` returns 0; error paths
    return 1/2), so on the happy path the function returns that int directly.
    Control-flow exceptions raised by typer's vendored click fork are mapped
    back to ints to preserve the argparse contract:
      * ``typer.Exit`` (also raised by ``--help``)      → its ``exit_code``
      * ``UsageError`` / ``NoArgsIsHelpError`` (unknown
        command, missing required option, no subcommand) → 2 (argparse parity)
      * ``Abort`` (Ctrl-C)                               → 1
    """
    configure_logging()
    try:
        result = app(args=argv, standalone_mode=False)
    except (typer.Exit, click.exceptions.Exit) as exc:
        return int(getattr(exc, "exit_code", 0))
    except (_TyperClickException, click.exceptions.ClickException) as exc:
        # Parse / usage / no-args-help errors: print to stderr and mirror
        # argparse's exit code 2 (UsageError.exit_code defaults to 2).
        exc.show()
        return int(getattr(exc, "exit_code", 1))
    except (typer.Abort, click.exceptions.Abort):
        return 1
    except SystemExit as exc:  # pragma: no cover - defensive
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    # Command bodies return their int exit code; a command returning nothing
    # (or any non-int) maps to success (0).
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
