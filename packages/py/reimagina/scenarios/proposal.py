"""One proposal: four numbers, five edits, and the values it refuses.

A planner proposes four things over a drawn area — storeys, square metres
per dwelling, people per household, lot coverage — and the platform stores
**five** edits
for them. That asymmetry is domain policy, not the shape of any one surface,
which is why it lives here rather than in the CLI that happened to need it
first. A router that re-derived it would re-introduce the precise failure the
spec calls out: planned floors frozen at the baseline while edificability
moves, because ``floors`` was written and ``num_levels`` was not.

The refusals are here for the same reason (``check_inputs``, over the one
registry of what a rule can set, ``RULE_INPUTS``). None is enforced by the
database — a rule's numbers are a jsonb object and ``check_ceilings``
documents degenerate inputs as the caller's job — so every surface that
authors a scenario has to make them, and the only way they stay the same
refusal is to be the same function.

**The messages name fields, not flags.** ``levels``, not ``--levels``: a
surface renders its own affordance, because an HTTP 400 that tells an API
client about a command-line flag is a message written for the wrong reader.
The exceptions carry the field name and the numbers so a caller can say it
however it needs to; ``apps/cli/scenario.py`` is the worked example.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from reimagina.scenarios.authoring import InvalidGeometry
from reimagina.scenarios.ceilings import (
    BlockInputs,
    FLOORS,
    CeilingBreach,
    check_ceilings,
    max_floors,
    max_levels_for,
)

if TYPE_CHECKING:
    from reimagina.indicators.catalog import IndicatorSpec

#: The ``feature_id`` the precheck gives its one synthetic block. No feature
#: has been selected yet — that happens at materialisation — so the string is
#: never shown; it exists because ``BlockInputs`` requires one.
_PROPOSAL = "(proposal)"


class ProposalValueError(ValueError):
    """A proposed value no indicator could be computed from, or not plausible.

    Carries ``field`` and ``value`` rather than only a message, so a surface
    can name the thing the way its own users do: ``--levels`` on a command
    line, ``levels`` in a request body.
    """

    def __init__(
        self, field: str, value: Any, *, must: str = "be a positive number"
    ) -> None:
        self.field = field
        self.value = value
        self.must = must
        super().__init__(f"{field} must {must}, got {value!r}")


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
        self,
        breaches: list[CeilingBreach],
        *,
        levels: float,
        unit_size: float,
        lot_coverage: float = 1.0,
    ) -> None:
        self.breaches = breaches
        self.levels = levels
        self.unit_size = unit_size
        # Both area-independent bounds: density scales with unit size and,
        # because it is judged on levels × coverage, inversely with coverage;
        # the floors bound does neither. The smaller is what a form may offer.
        # Without a unit size (a rule that keeps each block's own) only the
        # floors bound applies.
        self.allowed_levels = math.floor(
            min(max_levels_for(unit_size) / lot_coverage, max_floors())
            if unit_size > 0
            else max_floors()
        )
        # A unit size of 0 means none was given: naming "0 m²" would describe
        # a number the user never typed.
        at = f" at {unit_size:g} m² per dwelling" if unit_size > 0 else ""
        worst = breaches[0]
        if worst.indicator_slug == FLOORS:
            # The floors bound itself, not the combined minimum above, which
            # may be the density bound.
            advice = (
                f"The floors ceiling allows {math.floor(max_floors()):d} "
                f"storeys, whatever the unit size."
            )
        else:
            cover = f" and {lot_coverage:g} lot coverage" if lot_coverage < 1 else ""
            advice = (
                f"At {unit_size:g} m² per dwelling{cover} the ceiling allows "
                f"{self.allowed_levels:d} storeys, and the density part of it "
                f"rises in proportion to the unit size."
            )
        super().__init__(
            f"{levels:g} storeys{at} computes "
            f"{worst.would_be:.1f} for {worst.indicator_slug}, whose valid_max "
            f"is {worst.bound:.1f}. {advice} The per-block bounds (housing "
            f"units, population) depend on each block's area, so this check "
            f"cannot see them; materialisation checks them block by block."
        )


class EmptyProposal(ValueError):
    """A rule that sets no number at all — nothing to write."""

    def __init__(self) -> None:
        super().__init__(
            "a rule must set at least one of levels, unit_size, "
            "people_per_household, lot_coverage"
        )


@dataclass(frozen=True)
class RuleInput:
    """One number a rule can set: the attributes it writes and its range."""

    #: The override attributes the number is written to.
    attributes: tuple[str, ...]
    #: Exclusive lower bound.
    above: float
    #: Inclusive upper bound, or None.
    at_most: float | None
    #: How a refusal states the range.
    must: str


#: **The one registry of what a rule can set.** Its order is the order edits
#: are written in. Everything that knows the list of numbers — ``edits_for``,
#: ``check_inputs``, the API's ``inputs`` keys, ``reachable_indicators`` —
#: reads it from here.
#:
#: ``levels`` is the one number written to two attributes, ``floors`` and
#: ``num_levels``: the same quantity under the names two indicator families
#: read. Writing one and not the other made the map contradict itself
#: (planned floors frozen while edificability moved).
#:
#: The lower bound on ``unit_size`` and the upper bound on
#: ``people_per_household`` are plausibility bounds, not physics: 0.5 m² per
#: dwelling or 1,000 people per household were accepted and only failed later
#: as per-block ceiling breaches with a block id, not a field, in the message.
RULE_INPUTS: dict[str, RuleInput] = {
    "levels": RuleInput(("floors", "num_levels"), 0.0, None, "be a positive number"),
    "unit_size": RuleInput(
        ("unit_size",), 15.0, None, "be more than 15 m² per dwelling"
    ),
    "people_per_household": RuleInput(
        ("people_per_household",), 0.0, 15.0, "be between 0 and 15"
    ),
    "lot_coverage": RuleInput(
        ("lot_coverage",), 0.0, 1.0, "be a share between 0 and 1"
    ),
}


def edits_for(**values: float | None) -> list[dict[str, Any]]:
    """The override edits one rule writes: one per attribute of each number set.

    **Only the numbers given.** A number left out (or None) is not written, so
    every block keeps its own value for it. Writing all of them flattened the
    area: a rule meant to change household size also gave every block the
    same storeys, unit size and coverage — and density, which is
    ``coverage × levels × 10 000 / unit_size``, the same value everywhere.

    ⚠ A block whose baseline lacks one of the three edificability inputs is
    still skipped by ``edificability.py`` unless the rule sets it; the
    census-fitted baseline gives every Mexicali block all three.
    """
    # Names are not checked here: ``check_inputs`` refused unknown ones before
    # the rule was stored, and this only reads the registry's names.
    edits = [
        {"attribute_key": attribute, "value_numeric": float(value)}
        for name, spec in RULE_INPUTS.items()
        if (value := values.get(name)) is not None
        for attribute in spec.attributes
    ]
    if not edits:
        raise EmptyProposal()
    return edits


def check_inputs(inputs: Mapping[str, Any]) -> dict[str, float]:
    """A rule's numbers, refused or returned as floats in registry order.

    Unknown names, non-numbers, non-finite values and values outside each
    number's range (``RULE_INPUTS``) are a ``ProposalValueError`` naming the
    field; no number at all is an ``EmptyProposal``; then the area-independent
    ceilings (``precheck_ceilings``). Every surface that writes a rule goes
    through this, so they refuse the same things for the same reasons.
    """
    for name, value in inputs.items():
        if name not in RULE_INPUTS:
            raise ProposalValueError(
                name, value, must=f"be one of {', '.join(RULE_INPUTS)}"
            )
    checked: dict[str, float] = {}
    for name, spec in RULE_INPUTS.items():
        value = inputs.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProposalValueError(name, value, must="be a number")
        number = float(value)
        if (
            not math.isfinite(number)
            or number <= spec.above
            or (spec.at_most is not None and number > spec.at_most)
        ):
            raise ProposalValueError(name, number, must=spec.must)
        checked[name] = number
    if not checked:
        raise EmptyProposal()
    precheck_ceilings(
        checked.get("levels"),
        checked.get("unit_size"),
        checked.get("people_per_household"),
        checked.get("lot_coverage"),
    )
    return checked


def precheck_ceilings(
    levels: float | None,
    unit_size: float | None,
    people_per_household: float | None,
    lot_coverage: float | None = None,
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

    A rule that leaves a number out takes each block's own value for it, which
    only materialisation can see: without storeys there is nothing to check
    here, and without a unit size only the floors bound can speak.
    """
    if levels is None:
        return
    breaches = check_ceilings(
        [
            BlockInputs(
                feature_id=_PROPOSAL,
                levels=levels,
                # 0 makes check_ceilings skip the density bound, which needs
                # the unit size; the floors bound does not.
                unit_size=0.0 if unit_size is None else unit_size,
                people_per_household=people_per_household or 0.0,
                area_m2=0.0,
                lot_coverage=1.0 if lot_coverage is None else lot_coverage,
            )
        ]
    )
    if breaches:
        raise CeilingPrecheckFailed(
            breaches,
            levels=levels,
            unit_size=unit_size or 0.0,
            lot_coverage=1.0 if lot_coverage is None else lot_coverage,
        )


def polygon_wkt(geojson: Any) -> str:
    """The drawn area, as WKT, from parsed GeoJSON.

    Accepts a FeatureCollection, a Feature or a bare geometry, and unions
    several parts into one area: a selection made with two strokes is one
    rule, not two. Each part is repaired before the union, because GEOS
    overlay raises ``TopologyException`` on a self-intersecting stroke (a
    bowtie plus a second stroke was an HTTP 500); ``add_rule`` still runs
    ``ST_MakeValid`` on write. The one thing refused here is input with no
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
    polygon", and which ``add_rule`` raises for the same condition when
    PostGIS is the one to notice it. The original error is chained, because
    "a linearring requires at least 4 coordinates" is the useful half.
    """
    from shapely import force_2d, get_coordinates, make_valid
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
    # 2D: a third coordinate (QGIS, a GPS) would reach a 2D column as a 500.
    polygons = [
        force_2d(p) for p in parts if not p.is_empty and p.geom_type.endswith("Polygon")
    ]
    if not polygons:
        raise InvalidGeometry(
            "the drawn area carries no polygon: a scenario rule is an area, "
            "and a point or a line selects nothing"
        )
    # Longitude and latitude in degrees, or nothing: a polygon in projected
    # metres, or with a NaN vertex (which shapely silently drops), would be
    # stored as SRID 4326 and select nothing, read as a drawing mistake.
    coords = get_coordinates(polygons)
    if not (
        all(math.isfinite(c) for c in coords.ravel())
        and (abs(coords[:, 0]) <= 180).all()
        and (abs(coords[:, 1]) <= 90).all()
    ):
        raise InvalidGeometry(
            "the drawn area is not in longitude/latitude degrees "
            "(longitude within ±180, latitude within ±90, every vertex finite)"
        )
    # Only invalid parts: "structure" rewrites ring order even on valid
    # input. With keep_collapsed=False it keeps only polygonal output, so a
    # spike or a zero-area stroke cannot turn the union into a collection.
    repaired = [
        p if p.is_valid else make_valid(p, method="structure", keep_collapsed=False)
        for p in polygons
    ]
    kept = [p for p in repaired if not p.is_empty]
    if not kept:
        raise InvalidGeometry("the drawn area encloses no area once repaired")
    return str(unary_union(kept).wkt)


#: Units whose block values add up to something a planner reads as a total —
#: dwellings, people. Everything else (a density, a storey count, a 0–1 score)
#: is a rate, and a sum of rates is a number nobody can interpret.
_ADDITIVE_UNITS = frozenset({"units", "people"})


def reachable_indicators(
    registry: Sequence[IndicatorSpec] | None = None,
) -> list[str]:
    """The indicators a proposal can change, inputs before their dependents.

    **Derived, not listed.** The attributes come from ``RULE_INPUTS`` (what
    :func:`edits_for` writes) and the reach from ``impact.compute_dirty_set``
    — the same function that decides what a recompute runs — so the scenario
    workspace offers exactly what a rule moves. A list typed here would agree
    with the catalog until the day an indicator started reading
    ``num_levels``, and from then on hide it.
    """
    # Local imports: orchestration and the catalog import the scenario layer's
    # neighbours, and this is the one function here that needs either.
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import compute_dirty_set

    changed = {a for spec in RULE_INPUTS.values() for a in spec.attributes}
    units = compute_dirty_set(
        changed, (), SHIPPED_INDICATORS if registry is None else registry
    )
    return [unit.indicator for unit in units]


def table_statistic(unit: str | None) -> Literal["sum", "median"]:
    """The one number a summary table shows for an indicator with this unit.

    A total for counts, a median for rates. One rule, here, beside the list it
    describes, so every surface that tabulates a scenario agrees on what a
    cell means.
    """
    return "sum" if unit in _ADDITIVE_UNITS else "median"
