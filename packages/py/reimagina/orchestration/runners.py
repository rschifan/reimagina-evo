"""Concrete runner registry (component 7) — maps indicator slugs to compute fns.

The coordinator (component 8) sequences runners but does not know what computes
each indicator; this module supplies that binding for the shipped indicators.

The adapter forwards the dirty-set's ``features`` as ``feature_ids`` so the
compute fns recompute only the dirty features (true selective recompute), and
``aoi_id`` as a coarser fallback when no feature set is given (e.g. a full
backfill). The two compose at the SQL layer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import (
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    IndicatorSpec,
)
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.indicators.composite import compute_composite
from reimagina.orchestration.coordinator import Runner

# A compute fn: (conn, *, aoi_id, feature_ids, scenario_id, code_version) -> result.
IndicatorCompute = Callable[..., object]

# Direct indicators bind to their bespoke compute fn; composites all share the
# generic compute_composite (keyed by spec).
_DIRECT_COMPUTE: dict[str, IndicatorCompute] = {
    URBAN_FORM_BLOCK_COMPACTNESS.slug: compute_block_compactness,
    URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: compute_block_building_density,
}


def _scoped(compute_fn: IndicatorCompute) -> Runner:
    """Adapt a direct compute fn to the Runner protocol.

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


def _composite(spec: IndicatorSpec) -> Runner:
    """Adapt the generic composite compute to the Runner protocol, per spec."""

    def run(conn, *, features, scenario_id, code_version, aoi_id):
        return compute_composite(
            conn,
            spec=spec,
            feature_ids=list(features) or None,
            scenario_id=scenario_id,
            code_version=code_version,
            aoi_id=aoi_id,
        )

    return run


def default_runners(
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
) -> dict[str, Runner]:
    """Runner registry for every indicator the coordinator can run.

    Direct indicators use their bespoke compute fn; composites use the generic
    ``compute_composite`` (so a new composite needs only a catalog entry).
    """
    runners: dict[str, Runner] = {}
    for spec in registry:
        if spec.computation_kind == "composite":
            runners[spec.slug] = _composite(spec)
        elif spec.slug in _DIRECT_COMPUTE:
            runners[spec.slug] = _scoped(_DIRECT_COMPUTE[spec.slug])
    return runners
