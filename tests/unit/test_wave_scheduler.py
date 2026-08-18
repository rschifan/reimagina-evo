"""Unit tests for the wave scheduler — pure, no DB.

Covers the readiness rule an engine-owned scheduler would replace — plan_wave
— plus the run-status rollup it finalises with.
"""

from __future__ import annotations

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.port import overall_status
from reimagina.orchestration.wave import plan_wave


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
    # An empty report is the one case it does NOT delegate: no outcomes means
    # the dirty-set was empty, which is 'noop'. `overall_status` still answers
    # 'succeeded' there, because it rolls up units for a run that has some.
    assert RunReport(()).status == "noop"
    assert overall_status(()) == "succeeded"
