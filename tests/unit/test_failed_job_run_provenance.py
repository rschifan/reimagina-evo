"""Unit tests for durable failure provenance (DB-free, mocked).

`_record_failed_job_run` writes a `failed` job_run on its own connection so a
runner failure's provenance survives `compute_unit`'s rollback. Verified here
with stubs: correct model_family resolution, a failed completion, and — since it
is best-effort — that its own errors never propagate.
"""

from __future__ import annotations

from reimagina.orchestration import tasks


def test_record_failed_job_run_writes_failed_row(monkeypatch):
    calls: dict[str, dict] = {}

    class _Repo:
        def start_job(
            self, *, model_family, code_version, scenario_id, status, run_id=None
        ):
            calls["start"] = {
                "model_family": model_family,
                "code_version": code_version,
                "scenario_id": scenario_id,
                "status": status,
                "run_id": run_id,
            }
            return "jr-1"

        def complete_job(self, job_run_id, *, status, error_message=None):
            calls["complete"] = {
                "job_run_id": job_run_id,
                "status": status,
                "error_message": error_message,
            }

    class _Backend:
        def job_run_repo(self):
            return _Repo()

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            calls["committed"] = True

    monkeypatch.setattr(tasks, "queue_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(tasks, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(tasks, "backend_for_connection", lambda conn: _Backend())

    tasks._record_failed_job_run(
        run_id="run-7",
        indicator_slug="urban_form_block_planned_floors",
        scenario_id="s1",
        code_version="test",
        error="boom",
    )

    assert calls["start"]["status"] == "running"
    # model_family resolved from the shipped spec, not hard-coded
    assert calls["start"]["model_family"] == "urban_form"
    assert calls["complete"]["status"] == "failed"
    assert calls["complete"]["error_message"] == "boom"
    assert calls["committed"] is True
    # The run must be recorded even though this path runs from an `except`,
    # after the ambient `current_run` scope has already closed. A NULL run_id
    # here hides a failed unit from cancel and from the per-run timing query.
    assert calls["start"]["run_id"] == "run-7"


def test_record_failed_job_run_is_best_effort(monkeypatch):
    # A failure while recording provenance must never propagate — it would mask
    # the unit failure it is trying to record.
    def _boom(dsn):
        raise RuntimeError("db down")

    monkeypatch.setattr(tasks, "queue_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(tasks, "connect", _boom)

    tasks._record_failed_job_run(
        run_id="run-8",
        indicator_slug="unknown_slug",
        scenario_id=None,
        code_version="v",
        error="e",
    )  # does not raise
