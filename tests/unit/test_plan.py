"""Unit tests for the submit prelude — pure, no DB.

``resolve_change_set`` short-circuits on an explicit change-set without touching
the connection, so a None conn is enough here. The override-derived path needs
Postgres and is covered by ``tests/integration/test_scenario_whatif.py``.
"""

from __future__ import annotations

import logging

from reimagina.indicators.catalog import IndicatorSpec
from reimagina.orchestration.plan import noop_handle, plan_run


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
