"""Unit tests for the scenario change-set resolution (DB-free branches).

`resolve_change_set` decides whether to use the caller's explicit change-set or
derive one from the scenario's overrides. The branch selection is pure — only
the derive path touches the DB — so it is unit-testable with a stub.
"""

from __future__ import annotations

from reimagina.scenarios.overrides import resolve_change_set


def test_explicit_change_set_is_passed_through():
    # An explicit change-set wins and no derivation happens (conn untouched).
    changed, feats = resolve_change_set(
        None, changed_attributes=["floors"], edited_features=["f1"], scenario_id="s1"
    )
    assert changed == ["floors"]
    assert feats == ["f1"]


def test_baseline_without_scenario_passes_through_empties():
    changed, feats = resolve_change_set(
        None, changed_attributes=[], edited_features=[], scenario_id=None
    )
    assert changed == []
    assert feats == []


def test_none_inputs_coerced_to_lists():
    changed, feats = resolve_change_set(
        None, changed_attributes=None, edited_features=None, scenario_id=None
    )
    assert changed == []
    assert feats == []


def test_derives_from_overrides_when_change_set_omitted(monkeypatch):
    # scenario_id given + empty change-set → derive from the override rows.
    monkeypatch.setattr(
        "reimagina.scenarios.overrides.scenario_change_set",
        lambda conn, *, scenario_id: ({"floors"}, {"blk"}),
    )
    changed, feats = resolve_change_set(
        object(), changed_attributes=[], edited_features=[], scenario_id="s1"
    )
    assert changed == ["floors"]
    assert feats == ["blk"]
