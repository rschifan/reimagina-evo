"""One proposal: three numbers, four edits, and the values it refuses.

A planner proposes three things over a drawn area — storeys, square metres
per dwelling, people per household — and the platform stores **four** edits
for them. That asymmetry is domain policy, not the shape of any one surface,
which is why it lives here rather than in the CLI that happened to need it
first. A router that re-derived it would re-introduce the precise failure the
spec calls out: planned floors frozen at the baseline while edificability
moves, because ``floors`` was written and ``num_levels`` was not.

The two refusals are here for the same reason. Neither is enforced by the
database — ``value_numeric`` is a bare ``double precision`` and
``check_ceilings`` documents degenerate inputs as the caller's job — so every
surface that authors a scenario has to make them, and the only way they stay
the same refusal is to be the same function.

**The messages name fields, not flags.** ``levels``, not ``--levels``: a
surface renders its own affordance, because an HTTP 400 that tells an API
client about a command-line flag is a message written for the wrong reader.
The exceptions carry the field name and the numbers so a caller can say it
however it needs to; ``apps/cli/scenario.py`` is the worked example.
"""

from __future__ import annotations

import math
from typing import Any

from reimagina.scenarios.authoring import InvalidGeometry
from reimagina.scenarios.ceilings import (
    BlockInputs,
    FLOORS,
    CeilingBreach,
    check_ceilings,
    max_floors,
    max_levels_for,
)

#: The ``feature_id`` the precheck gives its one synthetic block. No feature
#: has been selected yet — that happens at materialisation — so the string is
#: never shown; it exists because ``BlockInputs`` requires one.
_PROPOSAL = "(proposal)"


class ProposalValueError(ValueError):
    """A proposed value no indicator could be computed from.

    Carries ``field`` and ``value`` rather than only a message, so a surface
    can name the thing the way its own users do: ``--levels`` on a command
    line, ``levels`` in a request body.
    """

    def __init__(self, field: str, value: float) -> None:
        self.field = field
        self.value = value
        super().__init__(f"{field} must be a positive number, got {value!r}")


class CeilingPrecheckFailed(ValueError):
    """Values the indicators could not store, refused before anything is read.

    Deliberately *not* ``authoring.CeilingExceeded``. That exception names a
    feature per breach because materialisation has blocks in hand; this
    refusal happens with no block chosen, and a message reading "feature
    (proposal) would compute…" would be worse than one that names the two
    numbers the user typed.

    The message is surface-neutral and says what the check does *not* cover,
    because a planner who gets past it must not read that as "this proposal
    is safe". What it does not say is how to fix it on any particular
    surface: ``breaches``, ``levels``, ``unit_size`` and ``allowed_levels``
    are carried so the caller can add that in its own words.
    """

    def __init__(
        self, breaches: list[CeilingBreach], *, levels: float, unit_size: float
    ) -> None:
        self.breaches = breaches
        self.levels = levels
        self.unit_size = unit_size
        # Both area-independent bounds: density scales with unit size, the
        # floors bound does not. The smaller is what a form may offer.
        self.allowed_levels = math.floor(min(max_levels_for(unit_size), max_floors()))
        worst = breaches[0]
        if worst.indicator_slug == FLOORS:
            advice = (
                f"The floors ceiling allows {self.allowed_levels:d} storeys at "
                f"{unit_size:g} m² per dwelling, whatever the unit size."
            )
        else:
            advice = (
                f"At {unit_size:g} m² per dwelling the ceiling allows "
                f"{self.allowed_levels:d} storeys, and the density part of it "
                f"rises in proportion to the unit size."
            )
        super().__init__(
            f"{levels:g} storeys at {unit_size:g} m² per dwelling computes "
            f"{worst.would_be:.1f} for {worst.indicator_slug}, whose valid_max "
            f"is {worst.bound:.1f}. {advice} The per-block bounds (housing "
            f"units, population) depend on each block's area, so this check "
            f"cannot see them; materialisation checks them block by block."
        )


def edits_for(
    levels: float, unit_size: float, people_per_household: float
) -> list[dict[str, Any]]:
    """The four edits one proposal writes, in the order they are written.

    Three numbers, four rules: ``levels`` sets both ``floors`` and
    ``num_levels``, which are the same quantity under two names
    (``seed_demo_attributes.py``). Writing one and not the other is the
    failure this function exists to prevent — ``urban_form_block_planned_floors``
    reads ``floors`` while the edificability family reads ``num_levels``, so a
    half-written proposal makes the map contradict itself: planned floors stay
    at the baseline while density moves.

    All three are required of the caller (not defaulted) because
    ``edificability.py`` skips any feature missing any of the three, and a
    scenario that silently computed nothing is the hardest failure to see.
    """
    return [
        {"attribute_key": "floors", "value_numeric": float(levels)},
        {"attribute_key": "num_levels", "value_numeric": float(levels)},
        {"attribute_key": "unit_size", "value_numeric": float(unit_size)},
        {
            "attribute_key": "people_per_household",
            "value_numeric": float(people_per_household),
        },
    ]


def require_positive(**values: float) -> None:
    """Refuse zero, negative and non-finite inputs.

    ``check_ceilings`` documents that degenerate inputs "yield 0 in the
    kernels and so cannot breach; a caller validates those separately" — this
    is that caller. Nothing downstream would refuse them: ``value_numeric`` is
    a plain ``double precision``, so ``levels=-3`` and ``levels=nan`` both
    reach ``scenario_override`` and then the compute.
    """
    for field, value in values.items():
        if not math.isfinite(value) or value <= 0:
            raise ProposalValueError(field, value)


def precheck_ceilings(
    levels: float, unit_size: float, people_per_household: float
) -> None:
    """Refuse a proposal the area-independent bounds already rule out.

    One synthetic block with ``area_m2=0``, which is a sentinel here meaning
    "no block chosen", not a measured area: no block has been chosen yet, so
    only the bound that is independent of area can speak, and passing a real
    or maximum area here would describe a block that does not exist — the
    reasoning ``ceilings`` gives for checking per block rather than per
    selection. ``check_ceilings`` rather than a local comparison so the bound
    comes from the catalog at call time, through the one kernel that
    materialisation also uses.
    """
    breaches = check_ceilings(
        [
            BlockInputs(
                feature_id=_PROPOSAL,
                levels=levels,
                unit_size=unit_size,
                people_per_household=people_per_household,
                area_m2=0.0,
            )
        ]
    )
    if breaches:
        raise CeilingPrecheckFailed(breaches, levels=levels, unit_size=unit_size)


def polygon_wkt(geojson: Any) -> str:
    """The drawn area, as WKT, from parsed GeoJSON.

    Accepts a FeatureCollection, a Feature or a bare geometry, and unions
    several parts into one area: a selection made with two strokes is one
    rule, not two. Validity is left to ``add_edit``, which repairs with
    ``ST_MakeValid`` on write — the one thing refused here is input with no
    polygonal part at all, because the error PostGIS gives for it arrives
    after a connection and a savepoint.

    Takes parsed GeoJSON rather than a path. A request body and a drawn file
    differ in where the bytes come from and in nothing else, so the reading
    stays with the surface that has the bytes and the unioning lives here.

    **Input that is not GeoJSON is a refusal, not a traceback.** Well-formed
    JSON that is not well-formed GeoJSON reaches shapely as four different
    exception types depending on how it is wrong — ``KeyError`` for a
    geometry with no ``coordinates``, ``GeometryTypeError`` for a type
    nobody has heard of, ``ValueError`` for a ring of two points,
    ``AttributeError`` for a bare string where an object was expected — and
    none of them is a refusal any surface should pass on raw. They become
    the one exception that already means this: ``InvalidGeometry``, whose
    docstring covers "the drawn area is not, and cannot be repaired into, a
    polygon", and which ``add_edit`` raises for the same condition when
    PostGIS is the one to notice it. The original error is chained, because
    "a linearring requires at least 4 coordinates" is the useful half.
    """
    from shapely.errors import ShapelyError
    from shapely.geometry import shape
    from shapely.ops import unary_union

    try:
        kind = geojson.get("type") if isinstance(geojson, dict) else None
        if kind == "FeatureCollection":
            geometries = [f.get("geometry") for f in geojson.get("features", [])]
        elif kind == "Feature":
            geometries = [geojson.get("geometry")]
        else:
            geometries = [geojson]
        parts = [shape(g) for g in geometries if g]
    except (AttributeError, KeyError, TypeError, ValueError, ShapelyError) as exc:
        raise InvalidGeometry(
            f"the drawn area could not be parsed as GeoJSON: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    polygons = [p for p in parts if not p.is_empty and p.geom_type.endswith("Polygon")]
    if not polygons:
        raise InvalidGeometry(
            "the drawn area carries no polygon: a scenario edit is an area, "
            "and a point or a line selects nothing"
        )
    return str(unary_union(polygons).wkt)
