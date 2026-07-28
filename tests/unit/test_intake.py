"""Unit tests for the intake entry point — pure, fake runners, no DB."""

from __future__ import annotations

from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_COMPOSITE,
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
    """A geometry edit dirties both direct indicators *and* the composite.

    This is the shipped DAG's only multi-wave path: the composite is not seeded
    by any attribute, it is pulled in as a downstream dependent, and it must run
    after both of its inputs.
    """
    log: list[str] = []
    report = evaluate_scenario(
        None,
        changed_attributes={"geometry"},
        edited_features={"b1"},
        runners={
            URBAN_FORM_BLOCK_COMPACTNESS.slug: _runner(log, "compactness"),
            URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: _runner(log, "density"),
            URBAN_FORM_BLOCK_COMPOSITE.slug: _runner(log, "composite"),
        },
    )
    assert report.status == "succeeded"
    assert set(log) == {"compactness", "density", "composite"}
    assert log[-1] == "composite"  # dependent runs after its inputs


def test_shipped_composite_skips_when_an_input_fails():
    """A composite cannot be trusted when a dirty input failed — F6."""

    def _boom(conn, **kwargs):
        raise RuntimeError("density blew up")

    log: list[str] = []
    report = evaluate_scenario(
        None,
        changed_attributes={"geometry"},
        edited_features={"b1"},
        runners={
            URBAN_FORM_BLOCK_COMPACTNESS.slug: _runner(log, "compactness"),
            URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: _boom,
            URBAN_FORM_BLOCK_COMPOSITE.slug: _runner(log, "composite"),
        },
    )
    outcomes = report.by_indicator()
    assert outcomes[URBAN_FORM_BLOCK_BUILDING_DENSITY.slug].status.value == "failed"
    assert outcomes[URBAN_FORM_BLOCK_COMPOSITE.slug].status.value == "skipped"
    assert "composite" not in log  # never ran
    assert report.status == "partial"  # compactness still succeeded


def test_evaluate_scenario_nothing_dirty_is_a_noop():
    """An attribute no indicator reads → empty dirty-set → ``noop``.

    Not ``succeeded``: nothing was invalidated, so no run happened. This used to
    report a vacuous success while the async orchestrators returned ``noop`` for
    the identical edit, so the answer depended on which entry point you used.
    """
    report = evaluate_scenario(
        None,
        changed_attributes={"owner_name"},
        edited_features={"b1"},
        runners={},
    )
    assert report.outcomes == ()
    assert report.status == "noop"


def test_the_sync_and_async_paths_agree_on_a_noop():
    """The whole point of the shared prelude: one answer, both entry points."""
    from reimagina.orchestration.synchronous import SynchronousOrchestrator

    kwargs = dict(changed_attributes={"owner_name"}, edited_features={"b1"})
    sync_report = evaluate_scenario(None, runners={}, **kwargs)
    handle = SynchronousOrchestrator(runners={}).submit_run(None, **kwargs)

    assert sync_report.status == "noop"
    assert handle.status is not None
    assert handle.status.status == "noop"
    assert handle.status.units == ()
