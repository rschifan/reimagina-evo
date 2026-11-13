"""The scenario-overlay predicate, at the level where its traps live.

`scenario_overlay` builds SQL, so most of it is only observable against a
database (tests/integration/test_scenario_read_overlay.py). Two properties are
not, and both are guards against bugs that were written once already.
"""

from __future__ import annotations

import pytest

from reimagina.storage._utils import scenario_overlay

SCENARIO = "44444444-4444-4444-4444-444444444444"


def test_a_bare_alias_is_required():
    """Correlation binding. Unqualified names inside the NOT EXISTS bind to the
    inner table first, so an empty alias silently yields
    `_ov.feature_id = _ov.feature_id` — always true — and every baseline row is
    excluded the moment any scenario row exists."""
    with pytest.raises(ValueError, match="bare table alias"):
        scenario_overlay("", SCENARIO)


def test_a_column_expression_is_rejected():
    """The sibling `scenario_filter` takes a *column expression* in this same
    position, so converting a call site by analogy is the likely mistake. It
    must fail at the call, not as `ir.scenario_id.scenario_id` when the query
    eventually runs."""
    with pytest.raises(ValueError, match="pass 'ir', not"):
        scenario_overlay("ir.scenario_id", SCENARIO)


def test_a_valueless_scenario_row_cannot_shadow_the_baseline():
    """The anti-join must test for a value, not merely for a row.

    `effective_numeric` resolves COALESCE(so.value_numeric, obs.value_numeric,
    base), and COALESCE skips a NULL override. Keyed on row existence alone, a
    `write_result(value_numeric=None, quality_flag='missing')` row suppresses
    the baseline *and* is dropped by the readers' own IS NOT NULL filters, so
    the feature disappears instead of falling back.
    """
    sql, params = scenario_overlay("ir", SCENARIO)
    assert "_ov.value_numeric IS NOT NULL" in sql
    assert "_ov.value_text IS NOT NULL" in sql
    assert "_ov.value_json IS NOT NULL" in sql
    assert params == [SCENARIO, SCENARIO], "one placeholder per branch"


def test_the_baseline_predicate_is_untouched():
    """A baseline read must stay the exact `IS NULL` test it always was — no
    subquery, no parameters."""
    sql, params = scenario_overlay("ir", None)
    assert sql == "ir.scenario_id IS NULL"
    assert params == []


def test_the_value_test_is_on_both_arms():
    """The two arms must be strict complements, or they both match one row.

    With the value test only in the probe, a valueless scenario row qualified
    through `scenario_id = X` while the baseline qualified through the
    anti-join that had just ignored that same row — so the pair was returned
    twice, which is the one outcome the anti-join exists to rule out. The
    equality arm therefore carries the same three-way test as the probe.

    Asserted on the SQL because the duplicate is what the readers see, and two
    of them hide it: `value_stats` filters the valueless row out with its own
    `IS NOT NULL`, and a dict keyed on `feature_id` collapses the pair.
    """
    sql, params = scenario_overlay("ir", SCENARIO)
    assert "ir.value_numeric IS NOT NULL" in sql
    assert "ir.value_text IS NOT NULL" in sql
    assert "ir.value_json IS NOT NULL" in sql
    assert params == [SCENARIO, SCENARIO], (
        "the arm's value test is a NULL check on the outer row, so it adds no "
        "placeholder"
    )
