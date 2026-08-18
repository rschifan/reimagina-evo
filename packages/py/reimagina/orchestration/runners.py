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

from reimagina.indicators.accessibility import ACCESSIBILITY_COMPUTE
from reimagina.indicators.building_density import compute_block_building_density
from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_DENSITY,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
    IndicatorSpec,
)
from reimagina.indicators.compactness import compute_block_compactness
from reimagina.indicators.composite import compute_composite
from reimagina.indicators.edificability import (
    compute_block_housing_density,
    compute_block_housing_units,
    compute_block_population,
)
from reimagina.indicators.planned_floors import compute_planned_floors
from reimagina.orchestration.coordinator import Runner

# A compute fn: (conn, *, aoi_id, feature_ids, scenario_id, code_version) -> result.
IndicatorCompute = Callable[..., object]

# Direct indicators bind to their bespoke compute fn; composites all share the
# generic compute_composite (keyed by spec).
_DIRECT_COMPUTE: dict[str, IndicatorCompute] = {
    URBAN_FORM_BLOCK_COMPACTNESS.slug: compute_block_compactness,
    URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: compute_block_building_density,
    URBAN_FORM_BLOCK_PLANNED_FLOORS.slug: compute_planned_floors,
    # Edificability: one calculation, three slugs. Each entry writes only its
    # own slug; every indicator is dispatched individually on both ops planes.
    EDIFICABILITY_BLOCK_HOUSING_UNITS.slug: compute_block_housing_units,
    EDIFICABILITY_BLOCK_POPULATION.slug: compute_block_population,
    EDIFICABILITY_BLOCK_DENSITY.slug: compute_block_housing_density,
    # Accessibility: six slugs across two families, all built from one spec-bound
    # closure, so the registry stays in step with the catalog automatically.
    **ACCESSIBILITY_COMPUTE,
}


def _recording(conn, slug: str, features, scenario_id, aoi_id):
    """The materialization context for one dispatched unit.

    Coverage is complete only when the run was neither feature-scoped nor
    AOI-scoped. A run over a subset genuinely did not finish the partition, and
    recording it as complete is the specific lie `coverage_scope` exists to
    prevent — `indicators/edificability.py` relies on partial writes, so this is
    a normal state rather than an error.
    """
    from reimagina.assets.keys import partition_key
    from reimagina.assets.materialize import materializing

    return materializing(
        conn,
        slug=slug,
        partition_key=partition_key({"scenario": scenario_id}),
        complete=not features and not aoi_id,
    )


def _record_outcome(outcome: dict, result: object) -> object:
    outcome["job_run_id"] = getattr(result, "job_run_id", None)
    outcome["row_count"] = getattr(result, "rows_written", None)
    return result


def _scoped(slug: str, compute_fn: IndicatorCompute) -> Runner:
    """Adapt a direct compute fn to the Runner protocol.

    The dirty-set's ``features`` become ``feature_ids`` (recompute only the
    dirty features); an empty set falls back to ``aoi_id`` scoping.

    Wrapping here rather than inside each compute body is deliberate: this is
    the one seam every dispatch passes through — the CLI resolves a runner from
    this registry, and so do the coordinator and the queue task — so a compute
    cannot be reached in a way that skips recording what it produced.
    """

    def run(conn, *, features, scenario_id, code_version, aoi_id):
        with _recording(conn, slug, features, scenario_id, aoi_id) as outcome:
            return _record_outcome(
                outcome,
                compute_fn(
                    conn,
                    aoi_id=aoi_id,
                    feature_ids=list(features) or None,
                    scenario_id=scenario_id,
                    code_version=code_version,
                ),
            )

    return run


def _composite(spec: IndicatorSpec) -> Runner:
    """Adapt the generic composite compute to the Runner protocol, per spec."""

    def run(conn, *, features, scenario_id, code_version, aoi_id):
        with _recording(conn, spec.slug, features, scenario_id, aoi_id) as outcome:
            return _record_outcome(
                outcome,
                compute_composite(
                    conn,
                    spec=spec,
                    feature_ids=list(features) or None,
                    scenario_id=scenario_id,
                    code_version=code_version,
                    aoi_id=aoi_id,
                ),
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
            runners[spec.slug] = _scoped(spec.slug, _DIRECT_COMPUTE[spec.slug])
    return runners
