"""Concrete runner registry (component 7) — maps indicator slugs to compute fns.

The coordinator (component 8) sequences runners but does not know what computes
each indicator; this module supplies that binding for the shipped indicators.

The adapter forwards the dirty-set's ``features`` as ``feature_ids`` so the
compute fns recompute only the dirty features (true selective recompute), and
``aoi_id`` as a coarser fallback when no feature set is given (e.g. a full
backfill). The two compose at the SQL layer.
"""

from __future__ import annotations

from collections.abc import Callable

from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import (
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
)
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.orchestration.coordinator import Runner

# A compute fn: (conn, *, aoi_id, feature_ids, scenario_id, code_version) -> result.
IndicatorCompute = Callable[..., object]


def _scoped(compute_fn: IndicatorCompute) -> Runner:
    """Adapt a compute fn to the Runner protocol.

    The dirty-set's ``features`` become ``feature_ids`` (recompute only the
    dirty features); an empty set falls back to ``aoi_id`` scoping.
    """

    def run(conn, *, features, scenario_id, code_version, aoi_id):
        return compute_fn(
            conn,
            aoi_id=aoi_id,
            feature_ids=list(features) or None,
            scenario_id=scenario_id,
            code_version=code_version,
        )

    return run


def default_runners() -> dict[str, Runner]:
    """The runner registry for every shipped indicator the coordinator can run."""
    return {
        URBAN_FORM_BLOCK_COMPACTNESS.slug: _scoped(compute_block_compactness),
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: _scoped(compute_block_building_density),
    }
