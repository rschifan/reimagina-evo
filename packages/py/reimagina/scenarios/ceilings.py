"""Authoring-time ceilings: refuse values the indicators could not store.

``curated_geo.indicator_result`` carries a trigger (r016) that RAISES when a
value exceeds the catalog's ``valid_max``. It does not clamp. A scenario that
proposes too many storeys therefore does not yield a capped number: it aborts
the whole compute, and takes down the composite that consumes it. This module
exists so the refusal happens when the scenario is authored, naming the block
and the number, rather than as a plpgsql exception halfway through a city-scale
run.

Three bounds are reachable from the authorable attributes (levels, unit size,
people per household):

* ``edificability_block_density`` = ``10000 * levels / unit_size``. Block area
  cancels, so this one is a property of the values alone.
* ``edificability_block_housing_units`` = ``area_m2 * levels / unit_size``.
* ``edificability_block_population`` = housing units * ``people_per_household``.

The last two scale with the block, so each block is checked with its own values
and its own area. Two edits can resolve to different values on different
blocks (six storeys in one district, twelve in another is an ordinary
proposal); a single triple plus the selection's maximum area describes no real
block, and would clear a breach in the taller district or refuse the shorter
one.

Bounds are never hardcoded: ``_bound_for`` reads ``valid_max`` from the shipped
catalog on every call, so the catalog stays the one place a bound lives.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from reimagina.indicators.catalog import SHIPPED_INDICATORS

DENSITY = "edificability_block_density"
HOUSING_UNITS = "edificability_block_housing_units"
POPULATION = "edificability_block_population"

_M2_PER_HECTARE = 10_000.0


@dataclass(frozen=True)
class BlockInputs:
    """The resolved values one block would be computed with."""

    feature_id: str
    levels: float
    unit_size: float
    people_per_household: float
    area_m2: float


@dataclass(frozen=True)
class CeilingBreach:
    """One (block, indicator) pair whose value would exceed ``valid_max``."""

    feature_id: str
    indicator_slug: str
    bound: float
    would_be: float


def _bound_for(slug: str) -> float:
    """``valid_max`` for ``slug``, read at call time so it cannot go stale."""
    for spec in SHIPPED_INDICATORS:
        if spec.slug == slug:
            if spec.valid_max is None:
                raise ValueError(f"indicator {slug!r} declares no valid_max")
            return float(spec.valid_max)
    raise KeyError(f"no shipped indicator {slug!r}")


def max_levels_for(unit_size: float) -> float:
    """Most storeys the density bound allows at ``unit_size`` m² per dwelling.

    The density bound alone: it is the only one independent of block area, so
    it is the only one a form can pre-check before any block is chosen.
    """
    return _bound_for(DENSITY) * unit_size / _M2_PER_HECTARE


def check_ceilings(blocks: Iterable[BlockInputs]) -> list[CeilingBreach]:
    """Every (block, indicator) whose proposed value exceeds its bound.

    Empty means every block passes all three. Degenerate inputs (non-positive
    levels, unit size or area) yield 0 in the kernels and so cannot breach; a
    caller validates those separately.
    """
    density_bound = _bound_for(DENSITY)
    units_bound = _bound_for(HOUSING_UNITS)
    pop_bound = _bound_for(POPULATION)
    breaches: list[CeilingBreach] = []
    for b in blocks:
        if b.levels <= 0 or b.unit_size <= 0:
            continue
        density = _M2_PER_HECTARE * b.levels / b.unit_size
        units = b.area_m2 * b.levels / b.unit_size if b.area_m2 > 0 else 0.0
        population = (
            units * b.people_per_household if b.people_per_household > 0 else 0.0
        )
        for slug, bound, value in (
            (DENSITY, density_bound, density),
            (HOUSING_UNITS, units_bound, units),
            (POPULATION, pop_bound, population),
        ):
            if value > bound:
                breaches.append(CeilingBreach(b.feature_id, slug, bound, value))
    return breaches
