"""Concrete runner registry (component 7) — maps indicator slugs to compute fns.

The coordinator (component 8) sequences runners but does not know what computes
each indicator; this module supplies that binding for the shipped indicators.

Rung-1 scoping caveat: the shipped compute fns are **AOI-scoped** — they
recompute every block in the AOI, not a specific feature set. So the adapter
drops the dirty-set's ``features`` for now and forwards ``aoi_id``. Feature-
level scoping lands when the compute fns accept a feature filter; the Runner
contract already carries ``features`` so that change is additive.
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

# An AOI-scoped compute fn: (conn, *, aoi_id, scenario_id, code_version) -> result.
AoiScopedCompute = Callable[..., object]


def _aoi_scoped(compute_fn: AoiScopedCompute) -> Runner:
    """Adapt an AOI-scoped compute fn to the Runner protocol.

    ``features`` is accepted (Runner contract) but unused until the compute
    fns support a feature filter — see module docstring.
    """

    def run(conn, *, features, scenario_id, code_version, aoi_id):  # noqa: ARG001
        return compute_fn(
            conn,
            aoi_id=aoi_id,
            scenario_id=scenario_id,
            code_version=code_version,
        )

    return run


def default_runners() -> dict[str, Runner]:
    """The runner registry for every shipped indicator the coordinator can run."""
    return {
        URBAN_FORM_BLOCK_COMPACTNESS.slug: _aoi_scoped(compute_block_compactness),
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug: _aoi_scoped(
            compute_block_building_density
        ),
    }
