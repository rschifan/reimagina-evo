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
