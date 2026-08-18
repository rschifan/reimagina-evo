"""The two guards that stop a missing input becoming a confident number.

Both helpers are pure so the interesting cases — including the ones that must
*not* raise — are testable without a database. The wiring into each compute is
covered by tests/integration/test_data_layer_contracts.py.
"""

from __future__ import annotations

import pytest

from reimagina.indicators._compute import (
    MissingInputData,
    require_measured,
    require_nonempty,
)


def test_empty_layer_refuses_rather_than_writing_zeros():
    with pytest.raises(MissingInputData) as exc:
        require_nonempty(
            False,
            indicator_slug="urban_form_block_building_density",
            needs="the global:building feature layer",
            loader="`bootstrap_overture_buildings_subset`",
        )
    message = str(exc.value)
    # The three things an operator needs: which indicator, what is missing,
    # and the command that fixes it. Asserted because a guard that raises
    # without the loader hint just moves the investigation, it doesn't end it.
    assert "urban_form_block_building_density" in message
    assert "global:building" in message
    assert "bootstrap_overture_buildings_subset" in message


def test_present_layer_passes():
    require_nonempty(
        True, indicator_slug="x", needs="y", loader="z"
    )  # must not raise


def test_layer_present_but_nothing_matched_in_scope_refuses():
    """The case require_nonempty cannot see.

    Buildings loaded for a different AOI, or loaded but never linked by
    `aoi compute-features`: the layer is non-empty, so the existence check
    passes, and every block still scores zero.
    """
    with pytest.raises(MissingInputData) as exc:
        require_measured(
            0,
            5273,
            indicator_slug="urban_form_block_building_density",
            what="buildings",
            hint="check the ingest bbox covers this AOI.",
        )
    assert "5273" in str(exc.value)
    assert "buildings" in str(exc.value)


def test_some_matched_passes():
    require_measured(
        1, 5273, indicator_slug="x", what="buildings", hint="h"
    )  # must not raise


def test_empty_scope_passes():
    """Nothing considered is not a data problem — an AOI filter can legitimately
    select no features, and raising there would make an empty selection an
    error rather than an empty result."""
    require_measured(0, 0, indicator_slug="x", what="buildings", hint="h")
