"""Unit tests for the synchronous orchestrator behind the port — pure, no DB.

A None connection makes runstore no-op (persistence skipped), so the
orchestrator runs entirely in-memory with fake runners here. The wave-scheduler
tests live in test_wave_scheduler.py.
"""

from __future__ import annotations

from reimagina.indicators.catalog import IndicatorSpec
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
