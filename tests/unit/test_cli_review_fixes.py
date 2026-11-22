from __future__ import annotations

import json
from contextlib import contextmanager


def test_perf_run_report_file_does_not_carry_the_dsn_password(monkeypatch, tmp_path):
    """The report file is the artefact people keep and pass to `perf compare`;
    only stdout went through the mask, so the file held the password."""
    from apps.cli import perf

    @contextmanager
    def fake_connect(dsn):
        yield object()

    monkeypatch.setattr(perf, "connect", fake_connect)
    monkeypatch.setattr(perf, "run_benchmarks", lambda conn, **kw: {"passed": True})
    out = tmp_path / "baseline.json"

    perf.run_perf_benchmarks(
        dsn="postgresql://u:s3cret@db/reimagina",
        dataset_tier="smoke",
        output_path=str(out),
    )

    text = out.read_text()
    assert "s3cret" not in text
    assert json.loads(text)["passed"] is True


def _stub_runner(monkeypatch, *, overlap_missing: bool):
    from reimagina.perf import runner

    monkeypatch.setattr(runner, "_require_postgres", lambda conn: None)
    monkeypatch.setattr(
        runner,
        "_dataset_summary",
        lambda conn: {"feature_rows": 10, "observation_rows": 10},
    )
    monkeypatch.setattr(runner, "_extent_bounds", lambda conn: (0.0, 0.0, 1.0, 1.0))
    monkeypatch.setattr(runner, "_get_feature_id", lambda conn: "f1")
    monkeypatch.setattr(runner, "_get_selected_feature_ids", lambda conn, count: ["f1"])
    monkeypatch.setattr(runner, "_get_aoi_id", lambda conn, logical_name: "a1")

    def overlap(conn):
        if overlap_missing:
            raise runner.OverlapBenchmarkDataMissing("AOI 'Cuauhtémoc' not found.")
        return [1, "a1", 2]

    monkeypatch.setattr(runner, "_build_overlap_block_ageb_cuauhtemoc_params", overlap)
    monkeypatch.setattr(
        runner,
        "_run_explain_analyze",
        lambda conn, **kw: {
            "execution_time_ms": 1.0,
            "actual_rows": 5000,
            "plan_rows": 1,
            "planning_time_ms": 0.1,
            "shared_hit_blocks": 0,
            "shared_read_blocks": 0,
            "temp_read_blocks": 0,
            "temp_written_blocks": 0,
            "node_types": [],
            "index_names": [],
            "relation_scans": [],
        },
    )
    return runner


def test_perf_run_with_no_benchmark_named_returns_a_report(monkeypatch):
    """The default selection was every declared spec, and the four tile specs
    have no parameter builder, so the run raised after doing the others and
    returned nothing. A DB without the Cuauhtémoc AOI aborted it too."""
    runner = _stub_runner(monkeypatch, overlap_missing=True)

    report = runner.run_benchmarks(object(), dataset_tier="smoke")

    ran = {row["benchmark"] for row in report["benchmarks"]}
    assert "bbox_features_small" in ran
    assert not any(name.startswith("tile_") for name in ran)
    assert [s["benchmark"] for s in report["skipped"]] == [
        "overlap_block_to_ageb_cuauhtemoc"
    ]


def test_perf_run_naming_a_tile_benchmark_says_why_it_cannot_run(monkeypatch):
    import pytest

    runner = _stub_runner(monkeypatch, overlap_missing=False)

    with pytest.raises(ValueError, match="no parameter builder"):
        runner.run_benchmarks(
            object(), dataset_tier="smoke", benchmark_names=["tile_z14_small"]
        )


# ── CP-03: a command whose payload says it failed exits non-zero ─────────


def _patch(monkeypatch, name, payload):
    monkeypatch.setattr(f"apps.cli.app.{name}", lambda **kwargs: payload)


def test_verify_schema_exits_1_when_a_relation_is_missing(monkeypatch, capsys):
    from apps.cli.app import main

    @contextmanager
    def fake_connect(dsn):
        yield object()

    monkeypatch.setattr("apps.cli.app.connect", fake_connect)
    monkeypatch.setattr(
        "apps.cli.app.verify_required_relations",
        lambda conn: {"curated_geo.feature": True, "serving_geo.feature_v": False},
    )
    assert main(["verify-schema"]) == 1
    assert json.loads(capsys.readouterr().out)["all_present"] is False

    monkeypatch.setattr(
        "apps.cli.app.verify_required_relations", lambda conn: {"x": True}
    )
    assert main(["verify-schema"]) == 0


def test_evaluate_scenario_exits_1_when_the_run_failed(monkeypatch, capsys):
    from apps.cli.app import main

    for status, code in [("failed", 1), ("partial", 1), ("succeeded", 0), ("noop", 0)]:
        _patch(monkeypatch, "evaluate_scenario_cli", {"status": status})
        assert main(["evaluate-scenario", "--sync"]) == code, status


def test_scenario_recompute_exits_1_when_the_evaluation_failed(monkeypatch, capsys):
    from apps.cli.app import main

    sid = "00000000-0000-0000-0000-000000000001"
    for status, code in [("failed", 1), ("cancelled", 1), ("succeeded", 0)]:
        _patch(
            monkeypatch, "recompute_scenario_cli", {"evaluation": {"status": status}}
        )
        assert main(["scenario", "recompute", "--scenario-id", sid]) == code, status


def test_perf_run_exits_1_when_a_benchmark_fails(monkeypatch, capsys):
    from apps.cli.app import main

    _patch(monkeypatch, "run_perf_benchmarks", {"passed": False, "benchmarks": []})
    assert main(["perf", "run"]) == 1
    _patch(monkeypatch, "run_perf_benchmarks", {"passed": True, "benchmarks": []})
    assert main(["perf", "run"]) == 0


def test_perf_compare_exits_1_on_a_failed_or_regressed_candidate(
    monkeypatch, capsys, tmp_path
):
    from apps.cli.app import main

    args = ["perf", "compare", str(tmp_path / "a"), str(tmp_path / "b")]
    _patch(
        monkeypatch,
        "compare_perf_reports",
        {"candidate_passed": False, "comparisons": []},
    )
    assert main(args) == 1
    _patch(
        monkeypatch,
        "compare_perf_reports",
        {"candidate_passed": True, "comparisons": [{"regressed": True}]},
    )
    assert main(args) == 1
    _patch(
        monkeypatch,
        "compare_perf_reports",
        {"candidate_passed": True, "comparisons": [{"regressed": False}]},
    )
    assert main(args) == 0


def test_aoi_commands_exit_2_for_an_unknown_aoi(monkeypatch, capsys):
    from apps.cli.app import main

    _patch(monkeypatch, "delete_aoi", {"status": "not_found"})
    assert main(["aoi", "delete", "--aoi-id", "x"]) == 2
    _patch(monkeypatch, "compute_aoi_features_cli", {"status": "not_found"})
    assert main(["aoi", "compute-features", "--aoi-id", "x"]) == 2
    _patch(monkeypatch, "delete_aoi", {"status": "deleted"})
    assert main(["aoi", "delete", "--aoi-id", "x"]) == 0


def test_assets_observe_exits_1_when_a_probe_failed(monkeypatch, capsys):
    from apps.cli.app import main

    _patch(monkeypatch, "assets_observe_cmd", {"failed": {"roads": "boom"}})
    assert main(["assets", "observe"]) == 1
    _patch(monkeypatch, "assets_observe_cmd", {"failed": {}})
    assert main(["assets", "observe"]) == 0


def test_overture_validate_fails_on_an_empty_layer(monkeypatch, capsys):
    """`count >= 0` held for every count, so validation could not fail."""
    from apps.cli.app import main
    from reimagina.pipeline import overture

    class Reader:
        def count_features(self, type_slug):
            return 0

        def get_features(self, type_slug, limit):
            return {"features": []}

    class Backend:
        def serving_reader(self):
            return Reader()

    monkeypatch.setattr(overture, "backend_for_connection", lambda conn: Backend())
    assert overture.validate_overture_buildings_layer(object())[
        "queryable_through_serving"
    ] is False

    _patch(monkeypatch, "validate_mexico_city_buildings", {"count": 0})
    assert main(["overture", "validate"]) == 1


# ── CP-09 / CP-10: user-input mistakes print `error: …`, not a traceback ──


def test_malformed_from_path_prints_why(capsys):
    """`main` swallowed the SystemExit message and exited 1 with no output."""
    from apps.cli.app import main

    code = main(["cdmx", "bootstrap", "--from-path", "/data/09.zip"])

    assert code == 2
    assert "LAYER=PATH" in capsys.readouterr().err


def test_a_malformed_bbox_is_an_error_line_not_a_traceback(capsys):
    from apps.cli.app import main

    code = main(["overture", "divisions", "--bbox", "1,2,3"])

    assert code == 2
    assert capsys.readouterr().err.startswith("error: bbox must be")


def test_a_non_uuid_id_is_an_error_line(monkeypatch, capsys):
    import psycopg2.errors

    from apps.cli.app import main

    def bad_uuid(**kwargs):
        raise psycopg2.errors.InvalidTextRepresentation(
            'invalid input syntax for type uuid: "x"'
        )

    monkeypatch.setattr("apps.cli.app.delete_aoi", bad_uuid)

    assert main(["aoi", "delete", "--aoi-id", "x"]) == 2
    assert "invalid input syntax for type uuid" in capsys.readouterr().err


def test_a_concurrent_zonal_stats_run_is_a_refusal(monkeypatch, capsys):
    """The wrapper built a friendly message, then let it escape as a traceback."""
    from apps.cli.app import main
    from reimagina.jobs.raster_zonal_stats import ConcurrentRasterRun

    def held(**kwargs):
        raise ConcurrentRasterRun("Another zonal-stats run holds the lock.")

    monkeypatch.setattr("apps.cli.app.run_zonal_stats", held)

    code = main(
        [
            "raster",
            "zonal-stats",
            "--source-id",
            "s",
            "--feature-type",
            "block",
            "--attribute-key-prefix",
            "lst",
        ]
    )
    assert code == 2
    assert "holds the lock" in capsys.readouterr().err
