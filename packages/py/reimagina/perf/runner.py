"""Benchmark execution, explain parsing, and report comparison."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from reimagina.storage import backend_for_connection

from .manifest import BENCHMARKS, DATASET_TIERS, BenchmarkSpec
from .seed import PERF_AOI_NAMES


def _require_postgres(conn) -> None:
    backend = backend_for_connection(conn)
    if backend.name != "postgres":
        raise ValueError("Performance benchmarks are Postgres/PostGIS-only.")


def _query_one(conn, sql: str, params: tuple | list | None = None):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()


def _dataset_summary(conn) -> dict[str, object]:
    feature_row = _query_one(
        conn,
        """
        SELECT
          COUNT(*)::bigint,
          COALESCE(ST_AsText(ST_Extent(geometry)::geometry), '')
        FROM curated_geo.feature
        """,
    )
    observation_row = _query_one(
        conn,
        """
        SELECT
          COUNT(*)::bigint,
          COUNT(*) FILTER (WHERE is_current)::bigint
        FROM curated_geo.observation
        """,
    )
    return {
        "feature_rows": int(feature_row[0]),
        "feature_extent_wkt": feature_row[1] or None,
        "observation_rows": int(observation_row[0]),
        "current_observation_rows": int(observation_row[1]),
    }


def _extent_bounds(conn) -> tuple[float, float, float, float]:
    row = _query_one(
        conn,
        """
        SELECT
          ST_XMin(extent::geometry),
          ST_YMin(extent::geometry),
          ST_XMax(extent::geometry),
          ST_YMax(extent::geometry)
        FROM (
          SELECT ST_Extent(geometry) AS extent
          FROM curated_geo.feature
        ) bounds
        """,
    )
    if row is None or any(value is None for value in row):
        raise ValueError("No canonical features are loaded. Benchmarks require feature data.")
    return tuple(float(value) for value in row)


def _bbox_from_extent(
    extent: tuple[float, float, float, float],
    *,
    width_ratio: float,
    height_ratio: float,
) -> tuple[float, float, float, float]:
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


def _polygon_wkt_from_bbox(bounds: tuple[float, float, float, float], *, complex_shape: bool) -> str:
    min_x, min_y, max_x, max_y = bounds
    if not complex_shape:
        return (
            f"POLYGON(({min_x} {min_y},"
            f"{max_x} {min_y},"
            f"{max_x} {max_y},"
            f"{min_x} {max_y},"
            f"{min_x} {min_y}))"
        )
    mid_x = min_x + (max_x - min_x) * 0.55
    inner_x = min_x + (max_x - min_x) * 0.35
    mid_y = min_y + (max_y - min_y) * 0.5
    top_y = min_y + (max_y - min_y) * 0.8
    return (
        f"POLYGON(({min_x} {min_y},"
        f"{max_x} {min_y},"
        f"{max_x} {max_y},"
        f"{mid_x} {max_y},"
        f"{mid_x} {top_y},"
        f"{inner_x} {mid_y},"
        f"{mid_x} {min_y},"
        f"{min_x} {min_y}))"
    )


def _get_feature_id(conn) -> str:
    row = _query_one(conn, "SELECT id FROM curated_geo.feature ORDER BY id LIMIT 1")
    if row is None:
        raise ValueError("No canonical features are loaded. Benchmarks require feature data.")
    return str(row[0])


def _get_selected_feature_ids(conn, *, count: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM curated_geo.feature ORDER BY id LIMIT %s", (count,))
        rows = cur.fetchall()
    if not rows:
        raise ValueError("No canonical features are loaded. Benchmarks require feature data.")
    return [str(row[0]) for row in rows]


def _get_aoi_id(conn, *, logical_name: str) -> str:
    aoi_name = PERF_AOI_NAMES[logical_name]
    row = _query_one(
        conn,
        """
        SELECT id
        FROM curated_geo.area_of_interest
        WHERE name = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (aoi_name,),
    )
    if row is None:
        raise ValueError(
            f"AOI {aoi_name!r} is missing. Run `perf seed-observations` first to prepare AOI benchmark data."
        )
    return str(row[0])


def _build_params(conn, *, benchmark_name: str, dataset_tier: str) -> list[object]:
    tier = DATASET_TIERS[dataset_tier]

    # Benchmarks that work off the real INEGI geometry resolve their own
    # references (AOI + type ids) and should not require the generic
    # _extent_bounds probe, which fails when synthetic fixture data is absent.
    if benchmark_name == "overlap_block_to_ageb_cuauhtemoc":
        return _build_overlap_block_ageb_cuauhtemoc_params(conn)

    extent = _extent_bounds(conn)
    if benchmark_name == "bbox_features_small":
        bounds = _bbox_from_extent(extent, width_ratio=0.02, height_ratio=0.02)
        return [*bounds, *bounds]
    if benchmark_name == "bbox_features_dense":
        bounds = _bbox_from_extent(extent, width_ratio=0.08, height_ratio=0.08)
        return [*bounds, *bounds]
    if benchmark_name == "polygon_features_small":
        polygon = _polygon_wkt_from_bbox(_bbox_from_extent(extent, width_ratio=0.03, height_ratio=0.03), complex_shape=False)
        return [polygon, polygon]
    if benchmark_name == "polygon_features_dense":
        polygon = _polygon_wkt_from_bbox(_bbox_from_extent(extent, width_ratio=0.1, height_ratio=0.08), complex_shape=True)
        return [polygon, polygon]
    if benchmark_name == "bbox_features_with_current_attributes":
        bounds = _bbox_from_extent(extent, width_ratio=0.08, height_ratio=0.08)
        return [*bounds, *bounds]
    if benchmark_name == "selected_features_current_observations":
        return [_get_selected_feature_ids(conn, count=tier.selected_feature_count)]
    if benchmark_name == "feature_detail_by_id":
        return [_get_feature_id(conn)]
    if benchmark_name == "aoi_membership_read":
        return [_get_aoi_id(conn, logical_name="bbox_dense")]
    raise ValueError(f"Unknown benchmark name: {benchmark_name!r}")


class OverlapBenchmarkDataMissing(RuntimeError):
    """Raised when the overlap benchmark can't find the CDMX/Cuauhtémoc prerequisites."""


def _build_overlap_block_ageb_cuauhtemoc_params(conn) -> list[object]:
    """Resolve (target_type_id, aoi_id, source_type_id) for the Cuauhtémoc overlap benchmark.

    Returns a list suitable for the _OVERLAP_DRYRUN_SQL placeholder order. Raises
    OverlapBenchmarkDataMissing with a clear message when the CDMX bootstrap
    has not been run, so the runner can decide between skip and fail.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM curated_geo.feature_type_catalog WHERE context = 'MEX' AND type_slug = 'block'"
        )
        row = cur.fetchone()
        if row is None:
            raise OverlapBenchmarkDataMissing(
                "MEX:block feature type not found. Run `reimagina-cli feat seed-types` first."
            )
        block_type_id = row[0]

        cur.execute(
            "SELECT id FROM curated_geo.feature_type_catalog WHERE context = 'MEX' AND type_slug = 'ageb'"
        )
        row = cur.fetchone()
        if row is None:
            raise OverlapBenchmarkDataMissing(
                "MEX:ageb feature type not found. Run `reimagina-cli feat seed-types` first."
            )
        ageb_type_id = row[0]

        cur.execute(
            "SELECT id FROM curated_geo.area_of_interest WHERE name = %s",
            ("Cuauhtémoc",),
        )
        row = cur.fetchone()
        if row is None:
            raise OverlapBenchmarkDataMissing(
                "AOI 'Cuauhtémoc' not found. Run `reimagina-cli cdmx bootstrap` followed by "
                "`reimagina-cli cdmx aoi-from-alcaldia --cve-mun 015 --name Cuauhtémoc` first."
            )
        aoi_id = row[0]

    # SQL placeholder order: target_type_id, aoi_id, source_type_id.
    return [ageb_type_id, aoi_id, block_type_id]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _walk_plans(plan: dict) -> list[dict]:
    nodes = [plan]
    for child in plan.get("Plans", []) or []:
        nodes.extend(_walk_plans(child))
    return nodes


def _load_explain_json(raw_value):
    if isinstance(raw_value, str):
        return json.loads(raw_value)
    return raw_value


def _summarize_plan(plan_doc) -> dict[str, object]:
    explain = _load_explain_json(plan_doc)
    root = explain[0]
    plan = root["Plan"]
    nodes = _walk_plans(plan)
    node_types = [node.get("Node Type") for node in nodes if node.get("Node Type")]
    index_names = sorted(
        {
            node["Index Name"]
            for node in nodes
            if node.get("Index Name")
        }
    )
    relation_scans = [
        {
            "node_type": node.get("Node Type"),
            "relation_name": node.get("Relation Name"),
            "alias": node.get("Alias"),
            "index_name": node.get("Index Name"),
        }
        for node in nodes
        if node.get("Relation Name")
    ]
    return {
        "planning_time_ms": float(root.get("Planning Time", 0.0)),
        "execution_time_ms": float(root.get("Execution Time", 0.0)),
        "actual_rows": int(plan.get("Actual Rows", 0)),
        "plan_rows": int(plan.get("Plan Rows", 0)),
        "shared_hit_blocks": int(plan.get("Shared Hit Blocks", 0)),
        "shared_read_blocks": int(plan.get("Shared Read Blocks", 0)),
        "temp_read_blocks": int(plan.get("Temp Read Blocks", 0)),
        "temp_written_blocks": int(plan.get("Temp Written Blocks", 0)),
        "node_types": node_types,
        "index_names": index_names,
        "relation_scans": relation_scans,
        "raw_plan": explain,
    }


def _run_explain_analyze(conn, *, sql: str, params: list[object], statement_timeout_ms: int) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = %s", (f"{statement_timeout_ms}ms",))
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
        plan_doc = cur.fetchone()[0]
    return _summarize_plan(plan_doc)


def _seq_scan_failure(spec: BenchmarkSpec, *, dataset_summary: dict[str, object], relation_scans: list[dict]) -> bool:
    large_tables = {
        "feature": int(dataset_summary.get("feature_rows", 0)) > 10_000,
        "observation": int(dataset_summary.get("observation_rows", 0)) > 10_000,
        "aoi_feature": int(dataset_summary.get("feature_rows", 0)) > 10_000,
    }
    for scan in relation_scans:
        if scan["relation_name"] in spec.hot_tables_no_seq_scan and scan["node_type"] in {"Seq Scan", "Parallel Seq Scan"}:
            if large_tables.get(scan["relation_name"], False):
                return True
    return False


def _large_hot_table_present(spec: BenchmarkSpec, *, dataset_summary: dict[str, object]) -> bool:
    table_sizes = {
        "feature": int(dataset_summary.get("feature_rows", 0)),
        "observation": int(dataset_summary.get("observation_rows", 0)),
        "aoi_feature": int(dataset_summary.get("feature_rows", 0)),
    }
    return any(table_sizes.get(table, 0) > 10_000 for table in spec.hot_tables_no_seq_scan)


def _evaluate_result(
    spec: BenchmarkSpec,
    *,
    dataset_tier: str,
    dataset_summary: dict[str, object],
    run_summaries: list[dict[str, object]],
) -> dict[str, object]:
    execution_times = [float(item["execution_time_ms"]) for item in run_summaries]
    p50 = _percentile(execution_times, 0.5)
    p95 = _percentile(execution_times, 0.95)
    p99 = _percentile(execution_times, 0.99)
    latest = run_summaries[-1]
    budget = spec.latency_budgets[dataset_tier]

    require_index_discipline = _large_hot_table_present(spec, dataset_summary=dataset_summary)
    expected_node_ok = True
    expected_index_ok = True
    if require_index_discipline:
        expected_node_ok = any(node in latest["node_types"] for node in spec.expected_node_any_of)
        if spec.expected_index_any_of:
            expected_index_ok = any(index in latest["index_names"] for index in spec.expected_index_any_of)
    row_count_ok = int(latest["actual_rows"]) >= spec.row_count_min
    seq_scan_ok = not _seq_scan_failure(spec, dataset_summary=dataset_summary, relation_scans=latest["relation_scans"])
    latency_ok = p50 <= budget.p50_ms and p95 <= budget.p95_ms and p99 <= budget.p99_ms

    checks = {
        "expected_node": expected_node_ok,
        "expected_index": expected_index_ok,
        "row_count": row_count_ok,
        "no_catastrophic_seq_scan": seq_scan_ok,
        "latency_budget": latency_ok,
    }
    return {
        "benchmark": spec.name,
        "description": spec.description,
        "checks": checks,
        "passed": all(checks.values()),
        "timing_ms": {
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "min": round(min(execution_times), 3),
            "max": round(max(execution_times), 3),
            "median": round(median(execution_times), 3),
        },
        "budget_ms": {
            "p50": budget.p50_ms,
            "p95": budget.p95_ms,
            "p99": budget.p99_ms,
        },
        "latest_plan": {
            "actual_rows": latest["actual_rows"],
            "plan_rows": latest["plan_rows"],
            "planning_time_ms": latest["planning_time_ms"],
            "execution_time_ms": latest["execution_time_ms"],
            "shared_hit_blocks": latest["shared_hit_blocks"],
            "shared_read_blocks": latest["shared_read_blocks"],
            "temp_read_blocks": latest["temp_read_blocks"],
            "temp_written_blocks": latest["temp_written_blocks"],
            "node_types": latest["node_types"],
            "index_names": latest["index_names"],
            "relation_scans": latest["relation_scans"],
        },
    }


def explain_benchmark(conn, *, benchmark_name: str, dataset_tier: str) -> dict[str, object]:
    _require_postgres(conn)
    if benchmark_name not in BENCHMARKS:
        raise ValueError(f"Unknown benchmark: {benchmark_name!r}")
    if dataset_tier not in DATASET_TIERS:
        raise ValueError(f"Unknown dataset tier: {dataset_tier!r}")

    spec = BENCHMARKS[benchmark_name]
    params = _build_params(conn, benchmark_name=benchmark_name, dataset_tier=dataset_tier)
    summary = _run_explain_analyze(
        conn,
        sql=spec.sql,
        params=params,
        statement_timeout_ms=spec.statement_timeout_ms,
    )
    return {
        "benchmark": benchmark_name,
        "dataset_tier": dataset_tier,
        "params": params,
        "plan": summary,
    }


def run_benchmarks(
    conn,
    *,
    dataset_tier: str,
    benchmark_names: list[str] | None = None,
    iterations: int | None = None,
) -> dict[str, object]:
    _require_postgres(conn)
    if dataset_tier not in DATASET_TIERS:
        raise ValueError(f"Unknown dataset tier: {dataset_tier!r}")

    tier = DATASET_TIERS[dataset_tier]
    selected_names = benchmark_names or list(BENCHMARKS.keys())
    dataset_summary = _dataset_summary(conn)

    results: list[dict[str, object]] = []
    for name in selected_names:
        if name not in BENCHMARKS:
            raise ValueError(f"Unknown benchmark: {name!r}")
        spec = BENCHMARKS[name]
        params = _build_params(conn, benchmark_name=name, dataset_tier=dataset_tier)
        run_summaries = [
            _run_explain_analyze(
                conn,
                sql=spec.sql,
                params=params,
                statement_timeout_ms=spec.statement_timeout_ms,
            )
            for _ in range(iterations or tier.iterations)
        ]
        results.append(
            {
                **_evaluate_result(
                    spec,
                    dataset_tier=dataset_tier,
                    dataset_summary=dataset_summary,
                    run_summaries=run_summaries,
                ),
                "params": params,
            }
        )

    passed = all(result["passed"] for result in results)
    return {
        "dataset_tier": dataset_tier,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_summary": dataset_summary,
        "benchmarks": results,
        "passed": passed,
    }


def compare_reports(*, baseline_path: str | Path, candidate_path: str | Path) -> dict[str, object]:
    baseline = json.loads(Path(baseline_path).read_text())
    candidate = json.loads(Path(candidate_path).read_text())
    baseline_map = {row["benchmark"]: row for row in baseline.get("benchmarks", [])}
    candidate_map = {row["benchmark"]: row for row in candidate.get("benchmarks", [])}
    all_names = sorted(set(baseline_map) | set(candidate_map))

    comparisons = []
    for name in all_names:
        base = baseline_map.get(name)
        cand = candidate_map.get(name)
        if base is None or cand is None:
            comparisons.append(
                {
                    "benchmark": name,
                    "status": "missing",
                    "baseline_present": base is not None,
                    "candidate_present": cand is not None,
                }
            )
            continue
        base_p95 = float(base["timing_ms"]["p95"])
        cand_p95 = float(cand["timing_ms"]["p95"])
        delta_ms = round(cand_p95 - base_p95, 3)
        delta_pct = round((delta_ms / base_p95) * 100, 2) if base_p95 else None
        comparisons.append(
            {
                "benchmark": name,
                "baseline_passed": bool(base.get("passed")),
                "candidate_passed": bool(cand.get("passed")),
                "baseline_p95_ms": base_p95,
                "candidate_p95_ms": cand_p95,
                "delta_p95_ms": delta_ms,
                "delta_p95_pct": delta_pct,
                "regressed": delta_ms > 0,
            }
        )

    return {
        "baseline_path": str(baseline_path),
        "candidate_path": str(candidate_path),
        "comparisons": comparisons,
        "candidate_passed": bool(candidate.get("passed")),
    }
