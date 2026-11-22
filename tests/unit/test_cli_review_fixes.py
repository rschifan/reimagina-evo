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
