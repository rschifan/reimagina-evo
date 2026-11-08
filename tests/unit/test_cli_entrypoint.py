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

    exit_code = main(
        ["bootstrap-db", "--app-dsn", "postgresql://localhost/reimagina_dev"]
    )

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
        lambda **kwargs: {
            "feature_stats": [{"type_slug": "building", "row_count": 1}],
            **kwargs,
        },
    )

    exit_code = main(["explore", "feature-stats"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["feature_stats"][0]["type_slug"] == "building"


def test_cli_explore_serving_stats_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_serving_stats",
        lambda **kwargs: {
            "serving_stats": [{"stat_scope": "all", "row_count": 1}],
            **kwargs,
        },
    )

    exit_code = main(["explore", "serving-stats"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["serving_stats"][0]["stat_scope"] == "all"


def test_cli_explore_executor_health_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_executor_health",
        lambda **kwargs: {
            "executor_health": [{"executor_id": "worker-a"}],
            **kwargs,
        },
    )

    exit_code = main(["explore", "executor-health"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executor_health"][0]["executor_id"] == "worker-a"


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

    exit_code = main(
        ["perf", "run", "--dataset-tier", "smoke", "--benchmark", "bbox_features_small"]
    )

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


def test_cli_feat_seed_types_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.seed_types",
        lambda **kwargs: {"seeded_count": 15, **kwargs},
    )

    exit_code = main(["feat", "seed-types"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["seeded_count"] == 15


def test_cli_overlap_declare_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.declare_overlap_pair",
        lambda **kwargs: {
            "pair": {"source": "MEX:lot", "target": "MEX:block"},
            **kwargs,
        },
    )

    exit_code = main(
        [
            "overlap",
            "declare-pair",
            "--source-context",
            "MEX",
            "--source-slug",
            "lot",
            "--target-context",
            "MEX",
            "--target-slug",
            "block",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pair"]["source"] == "MEX:lot"
    assert payload["source_slug"] == "lot"


def test_cli_evaluate_scenario_delegates(monkeypatch, capsys):
    """`evaluate-scenario` collects repeatable --changed-attribute / --feature
    and passes them to the orchestration intake helper."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.evaluate_scenario_cli",
        lambda **kw: captured.update(kw) or {"status": "succeeded", "outcomes": []},
    )
    exit_code = main(
        [
            "evaluate-scenario",
            "--dsn",
            "postgresql://localhost/x",
            "--changed-attribute",
            "geometry",
            "--feature",
            "b1",
            "--feature",
            "b2",
            "--aoi-id",
            "a1",
        ]
    )
    assert exit_code == 0
    assert captured["changed_attributes"] == ["geometry"]
    assert captured["edited_features"] == ["b1", "b2"]
    assert captured["aoi_id"] == "a1"
    assert captured["mode"] == "embedded"  # i.e. wait for the run to finish
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"


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
        lambda **kwargs: {
            "pairs": [{"source": "MEX:lot", "target": "MEX:block"}],
            **kwargs,
        },
    )

    exit_code = main(["overlap", "list-pairs"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pairs"][0]["target"] == "MEX:block"


def test_cli_raster_ingest_delegates(monkeypatch, capsys):
    """`raster ingest` passes every flag through to the pipeline helper."""
    received: dict = {}

    def _fake(**kwargs):
        received.update(kwargs)
        return {"source_id": "src", "layer_slug": kwargs["layer_slug"]}

    monkeypatch.setattr("apps.cli.app.ingest_raster", _fake)

    exit_code = main(
        [
            "raster",
            "ingest",
            "--raw-path",
            "/tmp/x.tif",
            "--source-name",
            "Test LST",
            "--provider",
            "NASA",
            "--version",
            "2026-01-15",
            "--layer-slug",
            "lst_2026_01_15",
            "--aggregation-kind",
            "intensive",
            "--colormap-name",
            "inferno",
            "--rescale",
            "270,320",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layer_slug"] == "lst_2026_01_15"
    assert received["source_name"] == "Test LST"
    assert received["aggregation_kind"] == "intensive"
    assert received["colormap_name"] == "inferno"
    assert received["published"] is True  # default


def test_cli_raster_ingest_unpublished_flag(monkeypatch, capsys):
    """--unpublished inverts to published=False at the pipeline call."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.ingest_raster",
        lambda **kw: captured.update(kw) or {"ok": True},
    )
    exit_code = main(
        [
            "raster",
            "ingest",
            "--raw-path",
            "/tmp/x.tif",
            "--source-name",
            "X",
            "--provider",
            "Y",
            "--version",
            "v1",
            "--layer-slug",
            "x",
            "--unpublished",
        ]
    )
    assert exit_code == 0
    assert captured["published"] is False


def test_cli_raster_list_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.list_raster_layers",
        lambda **kw: {"layers": [{"layer_slug": "a"}, {"layer_slug": "b"}], **kw},
    )
    exit_code = main(["raster", "list"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["layers"]) == 2


def test_cli_raster_publish_flips_flag(monkeypatch, capsys):
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.set_raster_published",
        lambda **kw: captured.update(kw) or {"ok": True, **kw},
    )

    exit_code = main(["raster", "publish", "--layer-slug", "my_slug"])
    assert exit_code == 0
    assert captured["published"] is True
    assert captured["layer_slug"] == "my_slug"

    captured.clear()
    exit_code = main(["raster", "publish", "--layer-slug", "my_slug", "--unpublish"])
    assert exit_code == 0
    assert captured["published"] is False


def test_cli_cdmx_aoi_from_alcaldia_defaults_to_cdmx(monkeypatch, capsys):
    """Without --cve-ent the CLI implicitly targets CDMX (CVE_ENT=09) for back-compat."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.aoi_from_alcaldia",
        lambda **kw: captured.update(kw) or {"status": "ok", **kw},
    )

    exit_code = main(
        [
            "cdmx",
            "aoi-from-alcaldia",
            "--cve-mun",
            "015",
            "--name",
            "Cuauhtémoc",
        ]
    )
    assert exit_code == 0
    assert captured["cve_ent"] == "09"
    assert captured["cve_mun"] == "015"


def test_cli_cdmx_aoi_from_alcaldia_passes_cve_ent_for_other_states(
    monkeypatch, capsys
):
    """--cve-ent 02 lets the same command target Mexicali (Baja California)."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.aoi_from_alcaldia",
        lambda **kw: captured.update(kw) or {"status": "ok", **kw},
    )

    exit_code = main(
        [
            "cdmx",
            "aoi-from-alcaldia",
            "--cve-ent",
            "02",
            "--cve-mun",
            "002",
            "--name",
            "Mexicali",
            "--project-name",
            "Reimagina pilots",
        ]
    )
    assert exit_code == 0
    assert captured["cve_ent"] == "02"
    assert captured["cve_mun"] == "002"
    assert captured["project_name"] == "Reimagina pilots"


def test_cli_cdmx_aoi_from_alcaldia_returns_error_when_missing(monkeypatch, capsys):
    """Returns exit code 2 when the function reports a missing municipality."""
    monkeypatch.setattr(
        "apps.cli.app.aoi_from_alcaldia",
        lambda **kw: {"status": "missing_municipality", **kw},
    )

    exit_code = main(
        [
            "cdmx",
            "aoi-from-alcaldia",
            "--cve-ent",
            "02",
            "--cve-mun",
            "002",
            "--name",
            "Mexicali",
        ]
    )
    assert exit_code == 2


def test_cli_cdmx_aoi_from_state_targets_whole_entidad(monkeypatch, capsys):
    """`cdmx aoi-from-state --cve-ent 09` builds a one-AOI 'all of CDMX'
    rather than a per-municipality row. No --cve-mun flag — the whole
    entidad polygon is the geometry."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.aoi_from_state",
        lambda **kw: captured.update(kw) or {"status": "ok", "aoi_id": "abc", **kw},
    )

    exit_code = main(
        [
            "cdmx",
            "aoi-from-state",
            "--cve-ent",
            "09",
            "--name",
            "CDMX (all)",
        ]
    )
    assert exit_code == 0
    assert captured["cve_ent"] == "09"
    assert captured["name"] == "CDMX (all)"
    # Critical: --cve-mun must not be required for state-level AOIs.
    assert "cve_mun" not in captured


def test_cli_cdmx_aoi_from_state_returns_error_when_missing(monkeypatch, capsys):
    """Mirrors the alcaldía error path: exit code 2 when the state
    feature isn't loaded yet."""
    monkeypatch.setattr(
        "apps.cli.app.aoi_from_state",
        lambda **kw: {"status": "missing_state", **kw},
    )

    exit_code = main(
        [
            "cdmx",
            "aoi-from-state",
            "--cve-ent",
            "99",
            "--name",
            "Nonexistent",
        ]
    )
    assert exit_code == 2


def test_cli_aoi_list_delegates(monkeypatch, capsys):
    """`aoi list` is the operator's first stop. It must hit list_aois
    with the supplied dsn and print whatever the helper returns."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.list_aois",
        lambda **kw: captured.update(kw) or {"aois": [], **kw},
    )
    exit_code = main(["aoi", "list", "--dsn", "postgresql://localhost/x"])
    assert exit_code == 0
    assert captured == {"dsn": "postgresql://localhost/x"}


def test_cli_aoi_delete_passes_aoi_id(monkeypatch, capsys):
    """`aoi delete` is the only CLI path to remove an AOI — must be
    reachable without raw SQL. Pinning the call shape here so a
    refactor that breaks it gets caught."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.delete_aoi",
        lambda **kw: captured.update(kw) or {"status": "deleted", **kw},
    )
    exit_code = main(
        [
            "aoi",
            "delete",
            "--dsn",
            "postgresql://localhost/x",
            "--aoi-id",
            "abc-123",
        ]
    )
    assert exit_code == 0
    assert captured == {"dsn": "postgresql://localhost/x", "aoi_id": "abc-123"}


def test_cli_aoi_compute_features_runs_all_when_no_aoi_id(monkeypatch, capsys):
    """`aoi compute-features` (no --aoi-id) reaches every AOI. Pins the
    fall-through default so a refactor that turns --aoi-id into required
    breaks the test, not the runbook."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.compute_aoi_features_cli",
        lambda **kw: captured.update(kw) or {"status": "ok", "aois": []},
    )
    exit_code = main(["aoi", "compute-features", "--dsn", "postgresql://localhost/x"])
    assert exit_code == 0
    assert captured == {"dsn": "postgresql://localhost/x", "aoi_id": None}


def test_cli_aoi_compute_features_passes_aoi_id_when_given(monkeypatch, capsys):
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.compute_aoi_features_cli",
        lambda **kw: captured.update(kw) or {"status": "ok", "aois": []},
    )
    exit_code = main(
        [
            "aoi",
            "compute-features",
            "--dsn",
            "postgresql://localhost/x",
            "--aoi-id",
            "abc-123",
        ]
    )
    assert exit_code == 0
    assert captured == {"dsn": "postgresql://localhost/x", "aoi_id": "abc-123"}


def test_cli_data_verify_sources_returns_nonzero_on_failure(monkeypatch, capsys):
    """`data verify-sources` is the pre-flight smoke test. It MUST exit
    non-zero on any failure so the runbook (and CI / make verify-sources)
    can fail-fast before the operator commits to an hour of ingest."""
    monkeypatch.setattr(
        "apps.cli.app.verify_sources",
        lambda **kw: {"all_ok": False, "checks": [{"label": "x", "ok": False}], **kw},
    )
    exit_code = main(["data", "verify-sources", "--release", "9999-01-01.0"])
    assert exit_code == 1


def test_cli_data_verify_sources_returns_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.verify_sources",
        lambda **kw: {"all_ok": True, "checks": [], **kw},
    )
    exit_code = main(["data", "verify-sources"])
    assert exit_code == 0


def test_cli_overture_extract_passes_release_and_bbox(monkeypatch, capsys):
    """`overture extract` must accept --release and --bbox so a fresh
    machine isn't trapped at the previously-hardcoded version. Pinning
    the call shape so a regression is caught at the CLI boundary."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.extract_mexico_city_buildings",
        lambda **kw: captured.update(kw) or {"output_path": kw["output_path"]},
    )
    # ``--bbox=-100,...`` (= form) avoids argparse misreading the leading
    # minus as a new flag — same trick operators must use in shell.
    exit_code = main(
        [
            "overture",
            "extract",
            "--release",
            "2026-04-15.0",
            "--bbox=-100.0,19.0,-99.0,20.0",
            "--output-path",
            "data/x.parquet",
        ]
    )
    assert exit_code == 0
    assert captured["release"] == "2026-04-15.0"
    assert captured["bbox"] == (-100.0, 19.0, -99.0, 20.0)


def test_cli_overture_load_passes_aoi_id(monkeypatch, capsys):
    """`overture load --aoi-id` triggers the polygon-clip step that
    prevents bbox-only orphans from persisting."""
    captured: dict = {}
    monkeypatch.setattr(
        "apps.cli.app.load_mexico_city_buildings",
        lambda **kw: captured.update(kw) or {"records_written": 0},
    )
    exit_code = main(
        [
            "overture",
            "load",
            "--dsn",
            "postgresql://localhost/x",
            "--raw-path",
            "data/x.parquet",
            "--release",
            "2026-04-15.0",
            "--source-name",
            "Overture Buildings - Mexicali",
            "--aoi-id",
            "uuid-1",
        ]
    )
    assert exit_code == 0
    assert captured["aoi_id"] == "uuid-1"
    assert captured["release"] == "2026-04-15.0"
    assert captured["source_name"] == "Overture Buildings - Mexicali"


def test_cli_assets_consumers_delegates(monkeypatch, capsys):
    """`assets consumers <key>` is the reverse question as a command.

    "I changed the street network — what must I recompute" now has an answer that
    does not require running anything.
    """
    monkeypatch.setattr(
        "apps.cli.app.assets_consumers_cmd",
        lambda **kwargs: {"related": [], **kwargs},
    )

    exit_code = main(["assets", "consumers", "global.road_network"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["asset_key"] == "global.road_network"


def test_cli_assets_observe_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.assets_observe_cmd",
        lambda **kwargs: {"changed": ["global.road_network"], **kwargs},
    )

    exit_code = main(["assets", "observe"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == ["global.road_network"]


def test_cli_assets_stale_delegates(monkeypatch, capsys):
    monkeypatch.setattr(
        "apps.cli.app.assets_stale_cmd",
        lambda **kwargs: {"count": 0, "assets": [], **kwargs},
    )

    exit_code = main(["assets", "stale"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_cli_bootstrap_db_reads_every_dsn_from_the_environment(monkeypatch, capsys):
    """`make migrate` takes no arguments, so the environment is the only way in.

    The three DSNs were module-level constants pinned at import
    (``postgresql://localhost/reimagina``, ``.../reimagina_test``,
    ``.../postgres``), so `bootstrap-db` consulted none of the variables the
    README and `.env.example` tell you to set. Two failures followed, and the
    quiet one is worse:

    - the admin connection carried no user and no password and could not be
      given either, so `make migrate` died with ``fe_sendauth: no password
      supplied`` on any Postgres that asks for one — the compose stack
      included, which sets ``POSTGRES_PASSWORD`` and no trust override
    - even with that fixed it migrated ``reimagina`` while the API read
      whatever ``DATABASE_URL`` named, reporting ``migrated: true`` over an
      app database that stayed empty

    ``DATABASE_URL`` and ``TEST_DATABASE_URL`` already existed and were simply
    not read here. ``DATABASE_URL_ADMIN`` is new: creating a database needs a
    connection to another one, and nothing in the project named it.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://someone@db.example:5432/app")
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql://someone@db.example:5432/app_test"
    )
    monkeypatch.setenv(
        "DATABASE_URL_ADMIN", "postgresql://someone@db.example:5432/postgres"
    )
    monkeypatch.setattr(
        "apps.cli.app.bootstrap_local_platform",
        lambda **kwargs: {"ok": True, **kwargs},
    )

    exit_code = main(["bootstrap-db"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["app_dsn"] == "postgresql://someone@db.example:5432/app"
    assert payload["test_dsn"] == "postgresql://someone@db.example:5432/app_test"
    assert payload["admin_dsn"] == "postgresql://someone@db.example:5432/postgres"


def test_cli_bootstrap_db_flags_still_beat_the_environment(monkeypatch, capsys):
    """An explicit flag outranks the environment, or `--admin-dsn` is a lie."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://env.example/app")
    monkeypatch.setenv("DATABASE_URL_ADMIN", "postgresql://env.example/postgres")
    monkeypatch.setattr(
        "apps.cli.app.bootstrap_local_platform",
        lambda **kwargs: {"ok": True, **kwargs},
    )

    exit_code = main(
        [
            "bootstrap-db",
            "--app-dsn",
            "postgresql://flag.example/app",
            "--admin-dsn",
            "postgresql://flag.example/postgres",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["app_dsn"] == "postgresql://flag.example/app"
    assert payload["admin_dsn"] == "postgresql://flag.example/postgres"


def test_cli_dsn_options_read_the_environment(monkeypatch, capsys):
    """Every `--dsn` option falls back to ``DATABASE_URL``.

    `bootstrap-db` was fixed first because `make migrate` exposed it, but the
    defect was never confined to that command: 48 options across the CLI took
    an import-time constant and consulted no variable at all. So on any
    PostgreSQL that asks for a password — the compose stack included — every
    inspection and ingest command failed with ``fe_sendauth: no password
    supplied``, and the only way through was to repeat ``--dsn`` on each
    invocation. Found by following this project's own operator documentation
    on a clean installation.

    An explicit flag still wins; this is a fallback, not an override.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://someone@db.example:5432/app")
    monkeypatch.setattr(
        "apps.cli.app.list_layers", lambda **kwargs: {"layers": [], **kwargs}
    )

    assert main(["explore", "layers"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dsn"] == "postgresql://someone@db.example:5432/app"


def test_cli_dsn_flag_beats_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env.example/app")
    monkeypatch.setattr(
        "apps.cli.app.list_layers", lambda **kwargs: {"layers": [], **kwargs}
    )

    assert main(["explore", "layers", "--dsn", "postgresql://flag.example/app"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dsn"] == "postgresql://flag.example/app"
