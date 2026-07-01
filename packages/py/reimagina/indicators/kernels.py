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
