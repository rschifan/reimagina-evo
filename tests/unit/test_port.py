"""Unit tests for next_ready_units + the synchronous orchestrator — pure, no DB.

A None connection makes runstore no-op (persistence skipped), so the synchronous
orchestrator runs entirely in-memory with fake runners here.
"""

from __future__ import annotations

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.coordinator import UnitStatus, next_ready_units
from reimagina.orchestration.impact import compute_dirty_set
from reimagina.orchestration.synchronous import SynchronousOrchestrator


def _spec(slug, *, reads=(), depends_on=(), kind="direct"):
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind=kind,
        value_type="numeric",
        aggregation_method="weighted_mean",
        applies_to=("block",),
        reads=reads,
        depends_on=depends_on,
    )


GREEN = _spec("green_coverage", reads=("land_use",))
WALK = _spec("walkability", reads=("land_use",))
LIVE = _spec(
    "liveability_index",
    kind="composite",
    depends_on=(("walkability", 0.5), ("green_coverage", 0.5)),
)
REG = (GREEN, WALK, LIVE)


def _runner(log, name, *, fail=False):
    def run(conn, *, features, scenario_id, code_version, aoi_id):
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    return run


def _dirty():
    return compute_dirty_set({"land_use"}, {"b1"}, REG)


def test_next_ready_units_initial_wave_is_the_seeds():
    ready = next_ready_units(_dirty(), REG, {})
    assert {u.indicator for u in ready} == {"green_coverage", "walkability"}


def test_next_ready_units_releases_composite_when_inputs_succeed():
    finished = {
        "green_coverage": UnitStatus.SUCCEEDED,
        "walkability": UnitStatus.SUCCEEDED,
    }
    ready = next_ready_units(_dirty(), REG, finished)
    assert {u.indicator for u in ready} == {"liveability_index"}


def test_next_ready_units_never_releases_on_failed_input():
    finished = {
        "green_coverage": UnitStatus.SUCCEEDED,
        "walkability": UnitStatus.FAILED,
    }
    assert next_ready_units(_dirty(), REG, finished) == []


def test_synchronous_orchestrator_runs_and_reports_status():
    log: list[str] = []
    runners = {
        s: _runner(log, s)
        for s in ("green_coverage", "walkability", "liveability_index")
    }
    handle = SynchronousOrchestrator(registry=REG, runners=runners).submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )
    assert handle.status is not None
    assert handle.status.status == "succeeded"
    assert {u.indicator for u in handle.status.units} == {
        "green_coverage",
        "walkability",
        "liveability_index",
    }
    assert log.index("green_coverage") < log.index("liveability_index")


def test_synchronous_orchestrator_reports_partial_on_failure():
    log: list[str] = []
    runners = {
        "green_coverage": _runner(log, "green_coverage"),
        "walkability": _runner(log, "walkability", fail=True),
        "liveability_index": _runner(log, "liveability_index"),
    }
    handle = SynchronousOrchestrator(registry=REG, runners=runners).submit_run(
        None, changed_attributes={"land_use"}, edited_features={"b1"}
    )
    assert handle.status.status == "partial"
    by_status = {u.indicator: u.status for u in handle.status.units}
    assert by_status["walkability"] == "failed"
    assert by_status["liveability_index"] == "skipped"


def test_get_run_is_none_without_persistence():
    orch = SynchronousOrchestrator(registry=REG, runners={})
    assert orch.get_run(None, "missing") is None
