"""Unit tests for next_ready_units + the synchronous orchestrator — pure, no DB.

A None connection makes runstore no-op (persistence skipped), so the synchronous
orchestrator runs entirely in-memory with fake runners here.
"""

from __future__ import annotations

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.coordinator import (
    UnitStatus,
    next_ready_units,
    overall_status,
    plan_wave,
)
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


# ── plan_wave (async distributor scheduler) ──────────────────────────────


def test_plan_wave_dispatches_the_seeds_first():
    units = [
        ("green_coverage", "queued"),
        ("walkability", "queued"),
        ("liveability_index", "queued"),
    ]
    plan = plan_wave(units, REG)
    assert set(plan.dispatch) == {"green_coverage", "walkability"}
    assert plan.skip == ()
    assert plan.final_status is None  # composite still waiting


def test_plan_wave_releases_composite_after_inputs_succeed():
    units = [
        ("green_coverage", "succeeded"),
        ("walkability", "succeeded"),
        ("liveability_index", "queued"),
    ]
    plan = plan_wave(units, REG)
    assert plan.dispatch == ("liveability_index",)
    assert plan.final_status is None  # composite now running


def test_plan_wave_skips_dependent_of_failed_input():
    units = [
        ("green_coverage", "succeeded"),
        ("walkability", "failed"),
        ("liveability_index", "queued"),
    ]
    plan = plan_wave(units, REG)
    assert plan.dispatch == ()
    assert plan.skip == ("liveability_index",)
    # green succeeded, walk failed, composite skipped → all terminal → final.
    assert plan.final_status == "partial"


def test_plan_wave_finalizes_when_all_terminal():
    units = [
        ("green_coverage", "succeeded"),
        ("walkability", "succeeded"),
        ("liveability_index", "succeeded"),
    ]
    plan = plan_wave(units, REG)
    assert plan.dispatch == ()
    assert plan.skip == ()
    assert plan.final_status == "succeeded"


def test_overall_status_rollup():
    assert overall_status(["succeeded", "succeeded"]) == "succeeded"
    assert overall_status(["succeeded", "failed"]) == "partial"
    assert overall_status(["failed", "skipped"]) == "failed"


def test_overall_status_cancelled():
    """A cancelled run must not report as failed — but a real failure wins."""
    assert overall_status(["cancelled"]) == "cancelled"
    assert overall_status(["cancelled", "skipped"]) == "cancelled"
    # a unit actually failed: reporting 'cancelled' would hide the error
    assert overall_status(["cancelled", "failed"]) == "failed"
    # any success still makes it partial, cancellation or not
    assert overall_status(["succeeded", "cancelled"]) == "partial"


def test_run_report_status_shares_the_rollup_rule():
    """RunReport used to re-implement the rule; it must now delegate."""
    from reimagina.orchestration.coordinator import RunReport, UnitOutcome, UnitStatus

    report = RunReport(
        (
            UnitOutcome("a", UnitStatus.SUCCEEDED),
            UnitOutcome("b", UnitStatus.FAILED),
        )
    )
    assert report.status == "partial"
    assert RunReport(()).status == "succeeded"
