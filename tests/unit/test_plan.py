"""Unit tests for the submit prelude — pure, no DB.

``resolve_change_set`` short-circuits on an explicit change-set without touching
the connection, so a None conn is enough here. The override-derived path needs
Postgres and is covered by ``tests/integration/test_scenario_whatif.py``.
"""

from __future__ import annotations

import logging

import pytest

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.plan import UnknownIndicators, noop_handle, plan_run


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


def _plan(**kw):
    kw.setdefault("changed_attributes", {"land_use"})
    kw.setdefault("edited_features", {"b1"})
    kw.setdefault("registry", REG)
    return plan_run(None, **kw)


def test_plan_derives_the_dirty_set_in_dependency_order():
    plan = _plan()
    slugs = [u.indicator for u in plan.units]
    assert set(slugs) == {"green_coverage", "walkability", "liveability_index"}
    assert slugs[-1] == "liveability_index"  # dependent after its inputs
    assert not plan.is_noop


def test_plan_carries_the_run_params_every_executor_forwards():
    """``params`` must stay keyword-compatible with run_dirty_set and
    runstore.create_run — both are called as ``**plan.params``."""
    plan = _plan(scenario_id="s1", aoi_id="a1", code_version="v9")
    assert plan.params == {
        "scenario_id": "s1",
        "aoi_id": "a1",
        "code_version": "v9",
    }


def test_params_defaults_match_the_port_signature():
    plan = _plan()
    assert plan.params == {
        "scenario_id": None,
        "aoi_id": None,
        "code_version": "dev",
    }


def test_an_unread_attribute_is_a_noop():
    plan = _plan(changed_attributes={"owner_name"})
    assert plan.units == []
    assert plan.is_noop


def test_a_noop_warns_once_naming_the_scenario(caplog):
    """The empty dirty-set is nearly always a mis-specified change-set, so it
    must not pass silently — it used to be logged in two places with two
    different messages, or not at all."""
    with caplog.at_level(logging.WARNING, logger="reimagina.orchestration.plan"):
        _plan(changed_attributes={"owner_name"}, scenario_id="s-42")
    assert len(caplog.records) == 1
    assert "s-42" in caplog.records[0].getMessage()


def test_a_real_dirty_set_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="reimagina.orchestration.plan"):
        _plan()
    assert caplog.records == []


def test_noop_handle_carries_a_terminal_noop_status():
    handle = noop_handle("run-1")
    assert handle.run_id == "run-1"
    assert handle.status is not None
    assert handle.status.status == "noop"
    assert handle.status.units == ()


# ── Explicit indicator scoping ───────────────────────────────────────────────


def test_named_indicators_skip_derivation_entirely():
    """The "recompute exactly these" request, which no change-set can express."""
    plan = plan_run(
        None,
        changed_attributes=[],
        edited_features=[],
        indicators=["urban_form_block_compactness"],
    )
    assert [u.indicator for u in plan.units] == ["urban_form_block_compactness"]
    assert not plan.is_noop


def test_named_indicators_come_back_in_dependency_order():
    """Caller order must not decide scheduling order.

    A composite named before its input would otherwise be dispatched first and
    read a value the run is about to replace.
    """
    plan = plan_run(
        None,
        changed_attributes=[],
        edited_features=[],
        indicators=["urban_form_block_composite", "urban_form_block_compactness"],
    )
    order = [u.indicator for u in plan.units]
    assert order.index("urban_form_block_compactness") < order.index(
        "urban_form_block_composite"
    )


def test_named_indicators_carry_the_run_params():
    """`RunPlan.params` is required and splatted by the synchronous engine."""
    plan = plan_run(
        None,
        changed_attributes=[],
        edited_features=[],
        scenario_id="s-1",
        aoi_id="a-1",
        code_version="v9",
        indicators=["urban_form_block_compactness"],
    )
    assert plan.params == {
        "scenario_id": "s-1",
        "aoi_id": "a-1",
        "code_version": "v9",
    }


def test_an_unknown_indicator_is_refused_at_submit():
    """Not left to fail inside the unit.

    An unregistered slug reaches `s_compute` as `LookupError: no runner
    registered`, which reads as one failed unit in an otherwise healthy run
    rather than as the typo it is.
    """
    with pytest.raises(UnknownIndicators) as excinfo:
        plan_run(
            None,
            changed_attributes=[],
            edited_features=[],
            indicators=["urban_form_block_compactness", "not_an_indicator"],
        )
    assert excinfo.value.unknown == ("not_an_indicator",)


def test_named_indicators_recompute_whole_indicators():
    """Empty `features` means "everything this indicator applies to".

    The original per-feature scope lived in `run_plan` and is released when the
    run ends, so a later retry is deliberately the wider recompute.
    """
    plan = plan_run(
        None,
        changed_attributes=[],
        edited_features=[],
        indicators=["urban_form_block_compactness"],
    )
    assert plan.units[0].features == frozenset()
