"""The ingest → recompute wiring.

Three properties, each of which has a way of silently not holding:

* the trigger is **opt-in** — a load without ``--recompute`` submits nothing;
* a load that wrote **no rows** submits nothing, because ON CONFLICT DO NOTHING
  means a re-run invalidates nothing;
* the submission names **no attributes** by default, so the dirty set is
  derived from the layers the load changed rather than from a proxy attribute;
* the submission is **not feature-scoped** — see ``submit_after_ingest``. An
  Overture load writes buildings while every shipped indicator applies to
  blocks, so passing the written ids would scope the recompute to features the
  compute functions never match: a run that touches nothing and reports
  success.

The orchestrator is stubbed at ``get_orchestrator``, which is the seam the
factory docstring names for exactly this.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.cli import overture as overture_cli
from apps.cli.indicators import submit_after_ingest


class _Recorder:
    """Captures the kwargs `submit_run` was called with."""

    def __init__(self, status: str = "queued") -> None:
        self.calls: list[dict] = []
        self._status = status

    def submit_run(self, conn, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            run_id="run-1",
            status=None if self._status == "queued" else SimpleNamespace(status="noop"),
        )


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator", lambda mode=None: rec
    )
    monkeypatch.setattr("apps.cli.indicators.connect", lambda _dsn: _NullConn())
    return rec


class _NullConn:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def test_submit_after_ingest_is_not_feature_scoped(recorder):
    """The property the docstring is about: an empty feature set, always.

    A non-empty one would read as a tighter recompute and be wrong — the ids a
    loader writes are buildings, and the indicators apply to blocks.
    """
    result = submit_after_ingest(dsn="postgresql:///x", changed_attributes=["geometry"])

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert tuple(call["edited_features"]) == ()
    assert call["changed_attributes"] == ["geometry"]
    # An ingest changes the baseline, not somebody's scenario.
    assert call["scenario_id"] is None
    assert result["status"] == "queued"
    assert result["run_id"] == "run-1"


def test_a_noop_reports_no_run_id(monkeypatch):
    """An ingest touching an attribute no indicator reads is a noop.

    The run id is suppressed for the same reason the scenario path suppresses
    it: no run row exists, so the id would 404 on lookup.
    """
    rec = _Recorder(status="noop")
    monkeypatch.setattr(
        "reimagina.orchestration.factory.get_orchestrator", lambda mode=None: rec
    )
    monkeypatch.setattr("apps.cli.indicators.connect", lambda _dsn: _NullConn())

    result = submit_after_ingest(
        dsn="postgresql:///x", changed_attributes=["population"]
    )

    assert result["status"] == "noop"
    assert result["run_id"] is None


def _stub_load(monkeypatch, records_written: int):
    monkeypatch.setattr(
        overture_cli,
        "bootstrap_overture_buildings_subset",
        lambda conn, **kwargs: {
            "ingestion_run_id": "ing-1",
            "records_written": records_written,
        },
    )
    monkeypatch.setattr(overture_cli, "connect", lambda _dsn: _NullConn())


def test_load_without_the_flag_submits_nothing(monkeypatch):
    """Opt-in. A load must never start a multi-hour recompute unasked."""
    _stub_load(monkeypatch, records_written=5_000)
    submitted: list = []
    monkeypatch.setattr(
        overture_cli, "submit_after_ingest", lambda **kw: submitted.append(kw)
    )

    result = overture_cli.load_mexico_city_buildings(dsn="x", raw_path="p")

    assert submitted == []
    assert "recompute" not in result


def test_load_with_the_flag_names_no_attributes(monkeypatch):
    """The default is now empty, and that is the improvement.

    It used to be ("geometry",) — true of writing footprints, but a proxy:
    three indicators read `geometry`, so reloading buildings also recomputed
    compactness, which never looks at a building. With nothing named, the dirty
    set comes from which layers the load actually changed.
    """
    _stub_load(monkeypatch, records_written=5_000)
    submitted: list = []
    monkeypatch.setattr(
        overture_cli,
        "submit_after_ingest",
        lambda **kw: submitted.append(kw) or {"status": "queued", "run_id": "run-1"},
    )

    result = overture_cli.load_mexico_city_buildings(
        dsn="x", raw_path="p", aoi_id="aoi-1", recompute=True
    )

    assert len(submitted) == 1
    assert submitted[0]["changed_attributes"] == []
    assert submitted[0]["aoi_id"] == "aoi-1"
    assert result["recompute"]["status"] == "queued"


def test_a_load_that_wrote_nothing_does_not_recompute(monkeypatch):
    """ON CONFLICT DO NOTHING: a re-run writes nothing and invalidates nothing.

    Recomputing a city to discover that is the expensive way to learn it.
    """
    _stub_load(monkeypatch, records_written=0)
    submitted: list = []
    monkeypatch.setattr(
        overture_cli, "submit_after_ingest", lambda **kw: submitted.append(kw)
    )

    result = overture_cli.load_mexico_city_buildings(
        dsn="x", raw_path="p", recompute=True
    )

    assert submitted == []
    assert result["recompute"]["status"] == "skipped"


def test_an_explicit_attribute_still_drives_the_real_dirty_set():
    """Not a mock: the actual catalog, through the actual impact engine.

    This is no longer the trigger's default — freshness is — but the explicit
    path has to keep working for a caller that knows something the graph does
    not, and a scenario what-if is exactly that caller.

    It also shows why the old default was too broad: `geometry` reaches three
    indicators, including compactness, which a building reload does not affect.
    """
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import compute_dirty_set

    units = compute_dirty_set(["geometry"], [], SHIPPED_INDICATORS)
    slugs = {u.indicator for u in units}

    assert "urban_form_block_compactness" in slugs
    assert "urban_form_block_building_density" in slugs
    assert "urban_form_block_composite" in slugs
    assert "urban_form_block_planned_floors" not in slugs
    # Not feature-scoped in, not feature-scoped out.
    assert all(u.features == frozenset() for u in units)


def test_coverage_threshold_defaults_agree_across_entry_points():
    """The same computation must not produce different data by entry point.

    run_raster_zonal_stats documents 0.0 and why: under exactextract's area
    weighting, writing a row for every polygon touching a non-nodata cell is the
    conservative answer, and the older 0.5 silently dropped manzanas smaller
    than one raster pixel. Four other entry points kept the 0.5, so a run
    invoked directly and the same run invoked through a recipe disagreed about
    which small blocks exist.
    """
    import inspect

    from apps.cli import raster as raster_cli
    from reimagina.jobs.raster_zonal_stats import run_raster_zonal_stats
    from reimagina.storage.postgres.repositories import (
        PostgresAggregationRecipeRepository,
    )

    def _default(fn, name):
        return inspect.signature(fn).parameters[name].default

    assert _default(run_raster_zonal_stats, "coverage_threshold") == 0.0
    assert _default(raster_cli.run_zonal_stats, "coverage_threshold") == 0.0
    assert (
        _default(
            PostgresAggregationRecipeRepository.register_recipe, "coverage_threshold"
        )
        == 0.0
    )
