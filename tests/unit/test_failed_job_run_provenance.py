"""Unit tests for durable failure provenance (DB-free, mocked).

``record_failed_job_run`` writes a ``failed`` job_run on its own connection so a
runner failure's provenance survives the executor's rollback of the compute
transaction. Verified here with stubs: correct model_family resolution, a failed
completion, the run id, and — since it is best-effort — that its own errors
never propagate.

The rollback survival itself is not mockable; it is asserted against a real
database in ``tests/integration/test_failed_unit_provenance.py``.
"""

from __future__ import annotations

from reimagina.jobs import _lifecycle


class _Repo:
    def __init__(self, calls: dict) -> None:
        self.calls = calls

    def start_job(
        self, *, model_family, code_version, scenario_id, status, run_id=None
    ):
        self.calls["start"] = {
            "model_family": model_family,
            "code_version": code_version,
            "scenario_id": scenario_id,
            "status": status,
            "run_id": run_id,
        }
        return "jr-1"

    def complete_job(self, job_run_id, *, status, error_message=None):
        self.calls["complete"] = {
            "job_run_id": job_run_id,
            "status": status,
            "error_message": error_message,
        }


def _stub(monkeypatch) -> dict:
    """Swap the connection and the backend for recorders; keep the real logic."""
    calls: dict[str, object] = {}

    class _Conn:
        dsn = "postgresql://recorded"

        def commit(self):
            calls["committed"] = True

        def close(self):
            calls["closed"] = True

    class _Backend:
        def job_run_repo(self):
            return _Repo(calls)

    monkeypatch.setattr(_lifecycle, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(_lifecycle, "backend_for_connection", lambda conn: _Backend())
    return calls


def test_it_writes_a_failed_row_for_the_run(monkeypatch):
    calls = _stub(monkeypatch)

    _lifecycle.record_failed_job_run(
        "postgresql://x",
        run_id="run-7",
        indicator_slug="urban_form_block_planned_floors",
        scenario_id="s1",
        code_version="test",
        error="boom",
    )

    assert calls["start"]["status"] == "running"
    # Resolved from the shipped spec, not hard-coded.
    assert calls["start"]["model_family"] == "urban_form"
    assert calls["complete"]["status"] == "failed"
    assert calls["complete"]["error_message"] == "boom"
    assert calls["committed"] is True
    # The run must be recorded even though this path runs from an `except`,
    # after the ambient `current_run` scope has already closed. A NULL run_id
    # here hides a failed unit from cancel and from the per-run timing query.
    assert calls["start"]["run_id"] == "run-7"


def test_a_composite_is_recorded_as_a_composite(monkeypatch):
    """Composites carry a NULL model_family (r016), and the column is NOT NULL.

    The fallback must be what the runner would have written, not a placeholder:
    the row exists to replace one that was lost, and `job_run_family_idx` groups
    on this column.
    """
    calls = _stub(monkeypatch)
    composite = next(
        s
        for s in _lifecycle.SHIPPED_INDICATORS
        if s.model_family is None  # the catalog's own marker for a composite
    )

    _lifecycle.record_failed_job_run(
        "postgresql://x",
        run_id="run-9",
        indicator_slug=composite.slug,
        scenario_id=None,
        code_version="test",
        error="boom",
    )

    assert calls["start"]["model_family"] == "composite"


def test_the_connection_is_closed_not_just_committed(monkeypatch):
    """psycopg2's connection context manager ends the transaction and leaves the
    socket open. This runs once per failed unit per run, so it must close."""
    calls = _stub(monkeypatch)

    _lifecycle.record_failed_job_run(
        "postgresql://x",
        run_id="run-7",
        indicator_slug="urban_form_block_compactness",
        scenario_id=None,
        code_version="test",
        error="boom",
    )

    assert calls["closed"] is True


def test_it_is_best_effort(monkeypatch):
    # A failure while recording provenance must never propagate — it would
    # replace the unit failure it is trying to record.
    def _boom(dsn):
        raise RuntimeError("db down")

    monkeypatch.setattr(_lifecycle, "connect", _boom)

    _lifecycle.record_failed_job_run(
        "postgresql://x",
        run_id="run-8",
        indicator_slug="unknown_slug",
        scenario_id=None,
        code_version="v",
        error="e",
    )  # does not raise
