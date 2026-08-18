"""Pure validation at the durable workflow boundary."""

from __future__ import annotations

import pytest

from reimagina.orchestration.dbos_app import validate_workflow_input


def _params():
    return {"code_version": "test", "scenario_id": None, "aoi_id": None}


def test_accepts_a_dag_and_ignores_dependencies_outside_the_dirty_set():
    validate_workflow_input(
        [
            {"slug": "base", "features": ["f1"], "deps": ["not-dirty"]},
            {"slug": "derived", "features": ["f1"], "deps": ["base"]},
        ],
        _params(),
    )


def test_rejects_a_cycle_before_it_becomes_a_stuck_workflow():
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_workflow_input(
            [
                {"slug": "a", "features": [], "deps": ["b"]},
                {"slug": "b", "features": [], "deps": ["a"]},
            ],
            _params(),
        )


def test_rejects_duplicate_units():
    with pytest.raises(ValueError, match="duplicate"):
        validate_workflow_input(
            [
                {"slug": "a", "features": [], "deps": []},
                {"slug": "a", "features": [], "deps": []},
            ],
            _params(),
        )
