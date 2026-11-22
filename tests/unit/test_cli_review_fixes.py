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
