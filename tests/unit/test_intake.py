"""Unit tests for the intake entry point — pure, fake runners, no DB."""

from __future__ import annotations

from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    IndicatorSpec,
)
from reimagina.orchestration.intake import evaluate_scenario


def _spec(slug, *, reads=(), depends_on=(), kind="direct", applies_to=("block",)):
    return IndicatorSpec(
        slug=slug,
        name=slug,
        computation_kind=kind,
        value_type="numeric",
        aggregation_method="weighted_mean",
        applies_to=applies_to,
        reads=reads,
        depends_on=depends_on,
    )


def _runner(log, name, *, fail=False):
    def run(conn, *, features, scenario_id, code_version, aoi_id):
        log.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    return run


def test_evaluate_scenario_composes_impact_then_coordinator():
    green = _spec("green_coverage", reads=("land_use",))
    liveability = _spec(
        "liveability_index", kind="composite", depends_on=(("green_coverage", 1.0),)
    )
    log: list[str] = []
    report = evaluate_scenario(
        None,
        changed_attributes={"land_use"},
        edited_features={"b1"},
        registry=(green, liveability),
        runners={
            "green_coverage": _runner(log, "green_coverage"),
            "liveability_index": _runner(log, "liveability_index"),
        },
    )
    assert report.status == "succeeded"
    assert log == ["green_coverage", "liveability_index"]  # input before dependent


def test_evaluate_scenario_defaults_to_shipped_registry():
    # No registry passed → SHIPPED_INDICATORS; a geometry edit dirties both.
    log: list[str] = []
    report = evaluate_scenario(
        None,
        changed_attributes={"geometry"},
        edited_features={"b1"},
        runners={
            URBAN_FORM_BLOCK_COMPACTNESS.slug: _runner(log, "compactness"),
            URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: _runner(log, "density"),
        },
    )
    assert report.status == "succeeded"
    assert set(log) == {"compactness", "density"}


def test_evaluate_scenario_nothing_dirty_is_an_empty_success():
    # An attribute no indicator reads → empty dirty-set → no-op success.
    report = evaluate_scenario(
        None,
        changed_attributes={"owner_name"},
        edited_features={"b1"},
        runners={},
    )
    assert report.outcomes == ()
    assert report.status == "succeeded"
