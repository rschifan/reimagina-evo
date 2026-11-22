"""planned_floors and the composite honour ``aoi_id`` like every other indicator.

The runner passes ``feature_ids=None`` with an ``aoi_id`` when the dirty set is
empty. These two used to ignore the AOI and write a row for every block in the
database, other projects' blocks included.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from reimagina.indicators import planned_floors


class _Results:
    def __init__(self, current: dict[str, dict[str, float]] | None = None):
        self.current = current or {}
        self.written: list[tuple[str, float]] = []

    def list_current_for_indicator(self, *, indicator_slug, scenario_id):
        return [
            {"feature_id": fid, "value_numeric": v, "feature_type": "block"}
            for fid, v in self.current.get(indicator_slug, {}).items()
        ]

    def bulk_write_numeric_results(self, rows, **_kw):
        self.written = list(rows)
        return len(self.written)


class _Features:
    def __init__(self, in_aoi: list[str]):
        self.in_aoi = in_aoi
        self.calls: list[dict] = []

    def geodesic_metrics_by_type(self, *, type_id, aoi_id=None, feature_ids=None):
        self.calls.append({"aoi_id": aoi_id, "feature_ids": feature_ids})
        ids = self.in_aoi if aoi_id else ["a", "b", "outside"]
        if feature_ids:
            ids = [i for i in ids if i in feature_ids]
        return [(i, 1.0, 1.0) for i in ids]


class _Backend:
    def __init__(self, results: _Results, features: _Features):
        self._results, self._features = results, features

    def indicator_result_repo(self):
        return self._results

    def feature_repo(self):
        return self._features


def _wire(monkeypatch, module, backend, entered: list[str]):
    @contextmanager
    def fake_job_run(*_a, **_kw):
        entered.append("job_run")
        yield "jr-1"

    monkeypatch.setattr(module, "backend_for_connection", lambda _c: backend)
    monkeypatch.setattr(module, "job_run", fake_job_run)
    if hasattr(module, "get_type_id"):
        monkeypatch.setattr(module, "get_type_id", lambda *_a: "block-type")


def test_planned_floors_reads_inside_the_job_run(monkeypatch):
    """A raising read must still leave a failed job_run row (see _compute.py)."""
    entered: list[str] = []
    _wire(monkeypatch, planned_floors, _Backend(_Results(), _Features([])), entered)
    monkeypatch.setattr(planned_floors, "require_seeded", lambda *_a: None)

    def boom(*_a, **_kw):
        raise RuntimeError("invalid input syntax for type double precision")

    monkeypatch.setattr(planned_floors, "effective_numeric", boom)

    with pytest.raises(RuntimeError):
        planned_floors.compute_planned_floors(object())
    assert entered == ["job_run"]
