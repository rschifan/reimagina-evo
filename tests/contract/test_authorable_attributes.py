"""Which attributes a scenario may author is derived, not listed."""

from __future__ import annotations

import pytest

from reimagina.assets import registry
from reimagina.assets.keys import AssetEdge
from reimagina.assets.registry import (
    NON_AUTHORABLE_REASONS,
    authorable_attributes,
    overridable_attributes,
)
from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec

_EXCLUDED = {"geometry", "poi_version", "network_version"}


def test_authorable_is_the_derived_set_minus_three() -> None:
    assert set(authorable_attributes()) == set(overridable_attributes()) - _EXCLUDED


def test_authorable_is_sorted() -> None:
    assert authorable_attributes() == tuple(sorted(authorable_attributes()))


def test_every_exclusion_states_why() -> None:
    assert set(NON_AUTHORABLE_REASONS) == _EXCLUDED
    assert all(reason.strip() for reason in NON_AUTHORABLE_REASONS.values())


def test_a_new_policy_attribute_appears_without_editing_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A subset assertion would pass on a hardcoded four-tuple, which is the
    # implementation this test exists to forbid. Grow the catalog instead.
    extra = IndicatorSpec(
        slug="x_block_probe",
        name="Probe",
        computation_kind="sql",
        value_type="numeric",
        aggregation_method="mean",
        applies_to=("block",),
        inputs=(AssetEdge("mex.block", attributes=("lot_coverage",)),),
    )
    assert "lot_coverage" not in authorable_attributes()
    monkeypatch.setattr(registry, "_shipped", lambda: (*SHIPPED_INDICATORS, extra))
    assert "lot_coverage" in authorable_attributes()
