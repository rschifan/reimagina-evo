from __future__ import annotations

from uuid import uuid4

import geopandas as gpd
from shapely.geometry import Polygon

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.perf import (
    build_observability_report,
    prepare_benchmark_dataset,
    run_benchmarks,
)


def test_postgres_perf_smoke_benchmarks_run(conn):
    feat.seed_feature_types(conn)

    source_id = cat.register_source(
        conn,
        name="perf_smoke_buildings",
        provider="pytest",
        storage_type="vector",
        integration_role="canonical_entity",
        storage_uri="generated://perf/smoke",
        version="v1",
    )
    run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="pytest",
        orchestrator_run_id=str(uuid4()),
        transformation_step="silver_to_curated",
        code_version="test",
    )
    type_id = feat.get_type_id(conn, "global", "building")

    gdf = gpd.GeoDataFrame(
        [
            {
                "id": f"building-{i}",
                "geometry": Polygon(
                    [
                        (0.001 * i, 0.001 * i),
                        (0.001 * i + 0.0004, 0.001 * i),
                        (0.001 * i + 0.0004, 0.001 * i + 0.0004),
                        (0.001 * i, 0.001 * i + 0.0004),
                    ]
                ),
            }
            for i in range(1, 40)
        ],
        crs="EPSG:4326",
    )
    inserted = feat.load_features(
        conn,
        gdf,
        source_id=source_id,
        ingestion_run_id=run_id,
        type_id=type_id,
        dataset_version="v1",
        source_record_ref_col="id",
    )
    assert inserted == len(gdf)
    cat.complete_ingestion_run(
        conn, run_id, status="succeeded", details={"records_written": inserted}
    )

    prepared = prepare_benchmark_dataset(conn, dataset_tier="smoke", feature_limit=20)
    assert prepared["observation_seed"]["records_written"] > 0
    repeated = prepare_benchmark_dataset(conn, dataset_tier="smoke", feature_limit=20)
    assert repeated["observation_seed"]["records_written"] >= 0

    report = run_benchmarks(
        conn,
        dataset_tier="smoke",
        benchmark_names=[
            "bbox_features_small",
            "selected_features_current_observations",
            "feature_detail_by_id",
            "aoi_membership_read",
        ],
        iterations=2,
    )

    assert report["passed"] is True
    assert len(report["benchmarks"]) == 4
    assert all(result["passed"] for result in report["benchmarks"])

    observability = build_observability_report(conn, limit=5)
    assert "extensions" in observability
    assert "relation_sizes" in observability
