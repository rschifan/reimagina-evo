"""Pure indicator kernels — the math, isolated from any I/O.

Split out so the Hamilton dataflow (``reimagina.indicators.dataflow``) and the
compute workers can both import the kernels without an import cycle. Each
compute module re-exports its kernel, so ``from reimagina.indicators.compactness
import polsby_popper`` keeps working.
"""

from __future__ import annotations

import math

_M2_PER_HECTARE = 10_000.0


def polsby_popper(*, area_m2: float, perimeter_m: float) -> float:
    """Compactness on [0, 1]. Returns 0 for degenerate shapes.

    Pure function so it can be unit-tested and reused outside the worker.
    Clamps to 1.0 on the top end to absorb floating-point noise that would
    otherwise trip the valid_max = 1 bound in the catalog.
    """
    if area_m2 <= 0 or perimeter_m <= 0:
        return 0.0
    value = (4.0 * math.pi * area_m2) / (perimeter_m * perimeter_m)
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def count_to_density(*, count: int, area_m2: float) -> float:
    """Buildings per hectare. Returns 0.0 for degenerate areas."""
    if area_m2 <= 0 or count < 0:
        return 0.0
    return count / (area_m2 / _M2_PER_HECTARE)


# ── Edificability ───────────────────────────────────────────────────────────
#
# The buildable-capacity arithmetic, as specified by the Reimagina team:
#
#     housing_units = (lot_size × num_levels) / unit_size
#     population    = housing_units × people_per_household
#     density       = housing_units / lot_size_in_hectares
#
# Computed here at *block* grain — `lot_size` is the block's geodesic area —
# because no cadastral lot layer exists yet. The formulas are unchanged; only
# the denominator's meaning is. The same three kernels serve a lot layer the
# day one lands, which is why they take an area rather than a feature.
#
# The identity worth recording: this is the same equation the collaborators'
# own pipeline applies through zoning coefficients, with `num_levels` playing
# the role of CUS (coeficiente de uso de suelo) and `unit_size` that of IVE
# (m² per dwelling) — their `density = CUS × 10_000 / IVE` expands to exactly
# `housing_units / area_ha`.


def housing_units(*, area_m2: float, num_levels: float, unit_size_m2: float) -> float:
    """Dwellings a parcel supports. Returns 0.0 for degenerate inputs.

    ``unit_size_m2`` is a divisor, so a zero or negative value would raise or
    silently invert the result; it is treated as degenerate rather than special
    cased at the call site.
    """
    if area_m2 <= 0 or num_levels <= 0 or unit_size_m2 <= 0:
        return 0.0
    return (area_m2 * num_levels) / unit_size_m2


def resident_population(*, housing_units: float, people_per_household: float) -> float:
    """Residents implied by a dwelling count. Returns 0.0 for degenerate inputs."""
    if housing_units <= 0 or people_per_household <= 0:
        return 0.0
    return housing_units * people_per_household


def bounded_distance(distance_m: float | None, *, cutoff_m: float) -> float:
    """A reported accessibility distance, clamped into ``[0, cutoff_m]``.

    ``None`` means nothing of that category was reachable, which reports as the
    cutoff rather than as NULL: both mean "no useful access", and a NULL would
    need its own rendering path in every consumer.

    The clamp is not decoration. ``pgr_drivingDistance``'s ``agg_cost`` can
    exceed the requested cutoff by a fraction through floating-point
    accumulation over many edges, and ``valid_max`` is enforced by a Postgres
    trigger that raises *per row* — so an unclamped value turns a rounding
    artefact into a failed run.
    """
    if distance_m is None:
        return cutoff_m
    if distance_m <= 0:
        return 0.0
    return min(float(distance_m), cutoff_m)


def housing_density(*, housing_units: float, area_m2: float) -> float:
    """Dwellings per hectare. Returns 0.0 for degenerate areas.

    Distinct from ``count_to_density`` despite the identical shape: that one
    counts buildings actually present, this one counts dwellings the envelope
    permits. Keeping them apart means a change to either definition cannot
    quietly move the other.
    """
    if area_m2 <= 0 or housing_units <= 0:
        return 0.0
    return housing_units / (area_m2 / _M2_PER_HECTARE)
