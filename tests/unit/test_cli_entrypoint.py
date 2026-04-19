from __future__ import annotations

import json

from apps.cli.app import main


def test_cli_status_prints_json(capsys):
    exit_code = main(["status"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["app"] == "cli"
    assert payload["status"] == "bootstrap-ready"


def test_cli_bootstrap_db_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.bootstrap_local_platform",
        lambda **kwargs: {"ok": True, **kwargs},
    )

    exit_code = main(["bootstrap-db", "--app-dsn", "postgresql://localhost/reimagina_dev"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["app_dsn"] == "postgresql://localhost/reimagina_dev"


def test_cli_explore_layers_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_layers",
        lambda **kwargs: {"layers": [{"logical_name": "layer_catalog"}], **kwargs},
    )

    exit_code = main(["explore", "layers"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layers"][0]["logical_name"] == "layer_catalog"


def test_cli_explore_sources_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_sources",
        lambda **kwargs: {"sources": [{"source_name": "test"}], **kwargs},
    )

    exit_code = main(["explore", "sources"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"][0]["source_name"] == "test"


def test_cli_explore_feature_stats_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_feature_stats",
        lambda **kwargs: {"feature_stats": [{"type_slug": "building", "row_count": 1}], **kwargs},
    )

    exit_code = main(["explore", "feature-stats"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["feature_stats"][0]["type_slug"] == "building"


def test_cli_explore_serving_stats_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_serving_stats",
        lambda **kwargs: {"serving_stats": [{"stat_scope": "all", "row_count": 1}], **kwargs},
    )

    exit_code = main(["explore", "serving-stats"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["serving_stats"][0]["stat_scope"] == "all"


def test_cli_overture_load_delegates(monkeypatch, capsys, tmp_path):
    raw_path = tmp_path / "buildings.parquet"
    raw_path.write_text("placeholder")

    monkeypatch.setattr(
        "apps.cli.app.load_mexico_city_buildings",
        lambda **kwargs: {"records_written": 7, **kwargs},
    )

    exit_code = main(["overture", "load", "--raw-path", str(raw_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["records_written"] == 7
    assert payload["raw_path"] == str(raw_path)


def test_cli_overture_extract_delegates(monkeypatch, capsys, tmp_path):
    output_path = tmp_path / "subset.parquet"

    monkeypatch.setattr(
        "apps.cli.app.extract_mexico_city_buildings",
        lambda **kwargs: {"format": "GeoParquet", **kwargs},
    )

    exit_code = main(["overture", "extract", "--output-path", str(output_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "GeoParquet"
    assert payload["output_path"] == str(output_path)


def test_cli_overture_validate_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.validate_mexico_city_buildings",
        lambda **kwargs: {"count": 12, **kwargs},
    )

    exit_code = main(["overture", "validate"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 12


def test_cli_perf_seed_observations_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.seed_observations",
        lambda **kwargs: {"records_written": 25, **kwargs},
    )

    exit_code = main(["perf", "seed-observations", "--dataset-tier", "smoke"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["records_written"] == 25
    assert payload["dataset_tier"] == "smoke"


def test_cli_perf_run_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.run_perf_benchmarks",
        lambda **kwargs: {"passed": True, "benchmarks": [], **kwargs},
    )

    exit_code = main(["perf", "run", "--dataset-tier", "smoke", "--benchmark", "bbox_features_small"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["benchmark_names"] == ["bbox_features_small"]


def test_cli_perf_explain_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.explain_query",
        lambda **kwargs: {"benchmark": "bbox_features_small", **kwargs},
    )

    exit_code = main(["perf", "explain", "bbox_features_small"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["benchmark"] == "bbox_features_small"


def test_cli_perf_report_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.performance_report",
        lambda **kwargs: {"report": {"extensions": {}}, **kwargs},
    )

    exit_code = main(["perf", "report"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"] == {"extensions": {}}


def test_cli_perf_compare_delegates(monkeypatch, capsys, tmp_path):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text("{}")
    candidate.write_text("{}")

    monkeypatch.setattr(
        "apps.cli.app.compare_perf_reports",
        lambda **kwargs: {"comparisons": [], **kwargs},
    )

    exit_code = main(["perf", "compare", str(baseline), str(candidate)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["baseline_path"] == str(baseline)
    assert payload["candidate_path"] == str(candidate)


def test_cli_tiles_manifest_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.tile_manifest",
        lambda **kwargs: {"layers": [{"layer_id": "admin_reference"}], **kwargs},
    )

    exit_code = main(["tiles", "manifest"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layers"][0]["layer_id"] == "admin_reference"


def test_cli_tiles_export_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.export_tile_snapshot",
        lambda **kwargs: {"layer_id": "admin_reference", **kwargs},
    )

    exit_code = main(["tiles", "export", "admin_reference"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layer_id"] == "admin_reference"


def test_cli_tiles_sources_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_tile_sources",
        lambda **kwargs: {"layers": [{"layer_id": "baseline_live"}], **kwargs},
    )

    exit_code = main(["tiles", "sources"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layers"][0]["layer_id"] == "baseline_live"


def test_cli_tiles_snapshots_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_tile_snapshots",
        lambda **kwargs: {"snapshots": [{"layer_id": "admin_reference"}], **kwargs},
    )

    exit_code = main(["tiles", "snapshots"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshots"][0]["layer_id"] == "admin_reference"


def test_cli_overlap_declare_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.declare_overlap_pair",
        lambda **kwargs: {"pair": {"source": "MEX:lot", "target": "MEX:block"}, **kwargs},
    )

    exit_code = main([
        "overlap", "declare-pair",
        "--source-context", "MEX", "--source-slug", "lot",
        "--target-context", "MEX", "--target-slug", "block",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pair"]["source"] == "MEX:lot"
    assert payload["source_slug"] == "lot"


def test_cli_overlap_compute_all_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.compute_overlaps_all",
        lambda **kwargs: {"rows_total": 42, **kwargs},
    )

    exit_code = main(["overlap", "compute-all", "--aoi-id", "abc"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows_total"] == 42
    assert payload["aoi_id"] == "abc"


def test_cli_overlap_list_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_overlap_pairs",
        lambda **kwargs: {"pairs": [{"source": "MEX:lot", "target": "MEX:block"}], **kwargs},
    )

    exit_code = main(["overlap", "list-pairs"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pairs"][0]["target"] == "MEX:block"
