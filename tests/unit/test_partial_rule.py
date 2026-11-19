"""A rule writes only the numbers the planner set.

Every number left out keeps each block's own value. Writing all of them, as
proposals did, flattened the area: a rule meant to change household size also
gave every block the same storeys, unit size and coverage, and so the same
density.
"""

from __future__ import annotations

import pytest

from reimagina.scenarios.proposal import (
    EmptyProposal,
    edits_for,
    precheck_ceilings,
)


def test_only_the_numbers_given_are_written() -> None:
    assert edits_for(people_per_household=5) == [
        {"attribute_key": "people_per_household", "value_numeric": 5.0}
    ]
    assert [e["attribute_key"] for e in edits_for(levels=3)] == ["floors", "num_levels"]
    assert [e["attribute_key"] for e in edits_for(unit_size=70, lot_coverage=0.5)] == [
        "unit_size",
        "lot_coverage",
    ]


def test_a_rule_with_no_numbers_is_refused() -> None:
    with pytest.raises(EmptyProposal):
        edits_for()


def test_the_area_independent_check_needs_storeys_and_unit_size() -> None:
    # Without both, density depends on each block's own value, which only
    # materialisation can see: nothing to refuse here.
    precheck_ceilings(levels=40, unit_size=None, people_per_household=None)
    precheck_ceilings(levels=None, unit_size=10, people_per_household=None)
