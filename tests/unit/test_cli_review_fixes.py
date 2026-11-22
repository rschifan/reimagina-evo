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
    monkeypatch.setattr(
        runner, "_extent_bounds", lambda conn, aoi_id=None: (0.0, 0.0, 1.0, 1.0)
    )
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
    assert (
        overture.validate_overture_buildings_layer(object())[
            "queryable_through_serving"
        ]
        is False
    )

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


def test_slow_queries_skips_pg_stat_statements_that_is_not_preloaded(monkeypatch):
    """`CREATE EXTENSION` without the preload leaves a view that raises
    "must be loaded via shared_preload_libraries", aborting `perf report`."""
    from reimagina.perf import observability

    monkeypatch.setattr(observability, "_require_postgres", lambda conn: None)
    monkeypatch.setattr(
        observability,
        "pg_extension_status",
        lambda conn: {
            "pg_stat_statements_available": True,
            "pg_stat_statements_preloaded": False,
        },
    )

    class NoQueries:
        def cursor(self):
            raise AssertionError("pg_stat_statements must not be read")

    assert observability.slow_queries(NoQueries()) == {
        "available": False,
        "queries": [],
    }


def test_pertask_simulation_never_beats_the_slot_bound():
    """Slot occupancy was reset per ready-batch, so a batch-2 node started on
    a slot still busy with batch 1: 9 equal tasks on 4 slots came out at 2
    units of time instead of 3."""
    from reimagina.perf.workloads import Node, Workload, simulate

    roots = tuple(Node(slug=f"r{i}", cost=1.0) for i in range(8))
    workload = Workload(
        id="t",
        description="t",
        nodes=(*roots, Node(slug="c", cost=1.0, deps=("r0",))),
    )
    unit = workload.nominal_seconds["c"]

    assert simulate(workload, slots=4, strategy="pertask") == 3 * unit
    assert simulate(workload, slots=4, strategy="wave") == 3 * unit


# ── CP-07 / CP-08: raster slugs and recipe ids that match nothing ────────


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.params = params

    def fetchone(self):
        return self.rows.get(self.params[0]) if self.params else None


class _FakeConn:
    def __init__(self, layer_ids_by_slug):
        self.layer_ids_by_slug = layer_ids_by_slug

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, **kw):
        return _FakeCursor(
            {slug: (layer_id,) for slug, layer_id in self.layer_ids_by_slug.items()}
        )

    def commit(self):
        pass


class _FakeRepo:
    def __init__(self, recipes=()):
        self.recipes = set(recipes)
        self.calls = []

    def set_published(self, **kw):
        self.calls.append(("set_published", kw))

    def get_recipe(self, recipe_id):
        return {"id": recipe_id} if recipe_id in self.recipes else None

    def set_enabled(self, **kw):
        self.calls.append(("set_enabled", kw))

    def delete_recipe(self, recipe_id):
        self.calls.append(("delete_recipe", recipe_id))

    def list_recipes(self, **kw):
        self.calls.append(("list_recipes", kw))
        return []

    def list_published_layers(self):
        return []


def _raster_fakes(monkeypatch, *, layers, recipes=()):
    from apps.cli import raster

    repo = _FakeRepo(recipes)

    class Backend:
        def raster_layer_repo(self):
            return repo

        def aggregation_recipe_repo(self):
            return repo

    monkeypatch.setattr(raster, "connect", lambda dsn: _FakeConn(layers))
    monkeypatch.setattr(raster, "backend_for_connection", lambda conn: Backend())
    return raster, repo


def test_raster_publish_refuses_a_slug_that_matches_nothing(monkeypatch):
    import pytest

    raster, repo = _raster_fakes(monkeypatch, layers={"lst_day": "L1"})

    with pytest.raises(ValueError, match="lst_dya"):
        raster.set_raster_published(dsn="d", layer_slug="lst_dya", published=True)
    assert repo.calls == []

    raster.set_raster_published(dsn="d", layer_slug="lst_day", published=True)
    assert repo.calls == [
        ("set_published", {"layer_slug": "lst_day", "published": True})
    ]


def test_derivation_enable_and_remove_refuse_an_unknown_recipe(monkeypatch):
    import pytest

    raster, repo = _raster_fakes(monkeypatch, layers={}, recipes={"r1"})

    with pytest.raises(ValueError, match="r2"):
        raster.set_derivation_enabled(dsn="d", recipe_id="r2", enabled=True)
    with pytest.raises(ValueError, match="r2"):
        raster.remove_derivation(dsn="d", recipe_id="r2")
    assert repo.calls == []

    raster.remove_derivation(dsn="d", recipe_id="r1")
    assert repo.calls == [("delete_recipe", "r1")]


def test_derivation_commands_resolve_an_unpublished_layer(monkeypatch):
    """`raster ingest --unpublished` is how a layer is staged; the resolver
    scanned only published layers and called it an unknown slug."""
    raster, repo = _raster_fakes(monkeypatch, layers={"wp_2020": "L9"})

    raster.list_derivations(dsn="d", raster_layer_slug="wp_2020")

    assert repo.calls == [
        ("list_recipes", {"input_raster_layer_id": "L9", "enabled_only": True})
    ]


# ── CP-05: benchmarks can be aimed at one city, not the whole extent ─────


def test_perf_aoi_id_takes_the_extent_from_that_aoi(monkeypatch):
    """The centre of every feature's extent lands between two pilot cities;
    `--aoi-id` puts the boxes and the perf AOIs inside one of them."""
    from reimagina.perf import runner, seed

    seen = []

    def query_one(conn, sql, params=None):
        seen.append(params)
        return (10.0, 20.0, 11.0, 21.0)

    monkeypatch.setattr(runner, "_query_one", query_one)

    params = runner._build_params(
        object(), benchmark_name="bbox_features_small", dataset_tier="smoke", aoi_id="A"
    )

    assert seen == [("A",)]
    assert 10.0 < params[0] < params[2] < 11.0

    class Conn:
        def cursor(self):
            return _FakeCursor({"A": (10.0, 20.0, 11.0, 21.0)})

    assert seed._feature_extent(Conn(), aoi_id="A") == (10.0, 20.0, 11.0, 21.0)
