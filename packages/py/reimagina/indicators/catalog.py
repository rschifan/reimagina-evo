"""Catalog seeds for every indicator shipped with the platform.

One row per indicator. Adding a new indicator to the catalog is always just
a call to `upsert_indicator` here — no migration, no table change. The catalog
is then read by workers, the API, and UI components that need
unit / bounds / aggregation-method metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reimagina.storage import backend_for_connection


@dataclass(frozen=True)
class IndicatorSpec:
    """Declarative registration for a single indicator.

    Mirrors the columns in curated_geo.indicator_catalog. Kept separate from
    the storage Protocol so that seeding does not require a live connection
    and so the shipped set of indicators has a single source of truth.
    """

    slug: str
    name: str
    computation_kind: str
    value_type: str
    aggregation_method: str
    applies_to: tuple[str, ...]
    description: str | None = None
    model_family: str | None = None
    unit: str | None = None
    valid_min: float | None = None
    valid_max: float | None = None
    # Input attribute keys this indicator reads off a feature. The impact
    # engine (reimagina.orchestration.impact) seeds the dirty-set from this:
    # a scenario that edits attribute X invalidates every indicator whose
    # `reads` contains X. Composite indicators read other indicators, not
    # attributes, so they leave this empty and declare `depends_on` instead.
    reads: tuple[str, ...] = field(default_factory=tuple)
    depends_on: tuple[tuple[str, float | None], ...] = field(default_factory=tuple)
    # Indicators sharing a run_group are computed by ONE dispatched unit rather
    # than one each. Purely an ops-plane concern: `indicator_catalog` has no such
    # column and `seed_catalog` never reads this.
    #
    # ⚠ Every member of a group MUST declare identical `reads` and identical
    # `depends_on`. The wave scheduler works on (slug, status) pairs and knows
    # nothing about groups, so members with differing dependencies could be
    # dispatched in *different* waves — enqueueing the group's child twice and
    # running one expensive body twice, which `s_compute`'s retries_allowed=False
    # exists to make impossible. Identical inputs make dirtiness all-or-nothing
    # by construction. Asserted in tests/unit/test_indicator_registry.py.
    run_group: str | None = None
    # Which executor queue this indicator's unit belongs on, so a minutes-long
    # network computation can be scheduled apart from millisecond arithmetic
    # instead of contending for the same slots.
    resource_class: str = "default"


URBAN_FORM_BLOCK_BUILDING_DENSITY = IndicatorSpec(
    slug="urban_form_block_building_density",
    name="Block Building Density",
    description=(
        "Buildings per hectare on a block. Counts Overture buildings whose "
        "centroid falls inside the block polygon and divides by block area "
        "(hectares). Aggregated to AGEB via area-weighted mean of child "
        "blocks — equivalent to (Σ buildings) / (Σ block area) when the "
        "children nest cleanly."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="buildings_per_ha",
    # Dense Asian mega-cities peak around 250/ha; 500 is a generous safety
    # cap that catches pathological bugs without constraining real data.
    valid_min=0.0,
    valid_max=500.0,
    aggregation_method="weighted_mean",
    # Derived from the block polygon (building centroids ÷ block area), so a
    # geometry edit invalidates it.
    reads=("geometry",),
    applies_to=("block", "ageb"),
)


URBAN_FORM_BLOCK_COMPACTNESS = IndicatorSpec(
    slug="urban_form_block_compactness",
    name="Block Compactness (Polsby-Popper)",
    description=(
        "Polsby-Popper compactness of the block polygon: 4π × area / perimeter². "
        "Ranges from 0 (arbitrarily jagged) to 1 (perfect circle). Aggregated to "
        "coarser grains via area-weighted mean through feature_overlap."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="ratio_0_1",
    valid_min=0.0,
    valid_max=1.0,
    aggregation_method="weighted_mean",
    # Polsby-Popper is a pure function of the block polygon, so a geometry
    # edit invalidates it.
    reads=("geometry",),
    # Indicator is produced directly at the block grain and rolled up to AGEB
    # via feature_overlap. Both grains appear in applies_to so indicator_result
    # rows land cleanly at either level.
    applies_to=("block", "ageb"),
)


URBAN_FORM_BLOCK_PLANNED_FLOORS = IndicatorSpec(
    slug="urban_form_block_planned_floors",
    name="Planned Floors",
    description=(
        "The effective number of floors planned on a block — the base value "
        "unless a scenario overrides the `floors` attribute. The first "
        "attribute-reading indicator, so an upzoning what-if (override floors) "
        "recomputes it while the baseline keeps the base data."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="floors",
    valid_min=0.0,
    valid_max=200.0,
    aggregation_method="weighted_mean",
    # Reads the `floors` attribute (effective = base ⊕ scenario override), so a
    # scenario edit to `floors` invalidates it via the impact engine.
    reads=("floors",),
    applies_to=("block",),
)


URBAN_FORM_BLOCK_COMPOSITE = IndicatorSpec(
    slug="urban_form_block_composite",
    name="Urban Form Composite",
    description=(
        "Weighted combination of block compactness and building density — the "
        "shipped composite. Its purpose is structural as much as analytical: "
        "without a composite the dirty-set is always a single wave, so nothing "
        "exercises dependency ordering, downstream propagation, or the "
        "skip-a-dependent-of-a-failed-input rule."
    ),
    computation_kind="composite",
    # Composites must have model_family NULL — indicator_catalog CHECK (r016).
    value_type="numeric",
    # Inputs are a 0–1 ratio and a 0–500/ha density, so the weighted sum tops
    # out near 250; 300 is a safety cap that catches pathological bugs.
    valid_min=0.0,
    valid_max=300.0,
    aggregation_method="weighted_mean",
    # Reads no attribute: a composite is invalidated through `depends_on`, not
    # by the impact engine's attribute seeding.
    depends_on=(
        (URBAN_FORM_BLOCK_COMPACTNESS.slug, 0.5),
        (URBAN_FORM_BLOCK_BUILDING_DENSITY.slug, 0.5),
    ),
    applies_to=("block",),
)


# ── Edificability ───────────────────────────────────────────────────────────
#
# Buildable capacity from construction and land-use attributes: the worked
# example the Reimagina collaborators supplied. Three outputs of one three-line
# calculation, which is exactly why they share a `run_group` — dispatching three
# units to perform one multiplication each is all overhead and no work.
#
# ⚠ All three declare the SAME `reads`, including `people_per_household`, which
# only `population` actually consumes. That is the group invariant, not an
# oversight: identical inputs are what guarantee the three are always dirty
# together and can never split across waves. The cost is that editing
# `people_per_household` also recomputes housing_units and density —
# over-approximate, never wrong, and the same trade `resolve_change_set`
# already makes.
#
# ⚠ None of them reads `geometry`, though all three divide by block area. A
# scenario cannot edit block geometry today, and declaring it would drag these
# into every geometry-seeded dirty set — including the conformance fixture's,
# whose assertion that all non-poison units end `skipped` is meant to read
# identically on both ops-plane branches.

_EDIFICABILITY_READS = ("num_levels", "unit_size", "people_per_household")
_EDIFICABILITY_GROUP = "edificability"


EDIFICABILITY_BLOCK_HOUSING_UNITS = IndicatorSpec(
    slug="edificability_block_housing_units",
    name="Housing Units (buildable)",
    description=(
        "Dwellings the block's envelope supports: (block area × num_levels) / "
        "unit_size. Block area stands in for lot area until a cadastral lot "
        "layer exists — the formula is unchanged, only the denominator's grain."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="units",
    # ⚠ Sized from real data, after an assumption failed. This was 10,000, from
    # "a CDMX manzana tops out near 4 ha" — true of the dense central alcaldías,
    # false of Tlalpan, where manzanas reach 3.09 km² (median 5,944 m², p99
    # 194,664 m²). The real maximum over 5,273 Tlalpan blocks is 138,695, and
    # the bound rejected 71 of them.
    #
    # The cap's job is catching pathological input — a unit_size given in cm²
    # would land 10^4 too high — not encoding what is architecturally plausible.
    # A 3 km² manzana built out at 6 levels is a silly *result*, but it is a
    # faithful application of the formula to a rural block, and the formula is
    # a declared placeholder.
    valid_min=0.0,
    valid_max=200_000.0,
    aggregation_method="weighted_mean",
    reads=_EDIFICABILITY_READS,
    applies_to=("block",),
    run_group=_EDIFICABILITY_GROUP,
)


EDIFICABILITY_BLOCK_POPULATION = IndicatorSpec(
    slug="edificability_block_population",
    name="Resident Population (buildable)",
    description=(
        "Residents implied by the buildable dwelling count: housing_units × "
        "people_per_household."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="people",
    # housing_units cap × a generous household size. Measured maximum over the
    # 5,273 Tlalpan blocks is 499,302 — see the note on housing_units for why
    # the earlier figure was too small.
    valid_min=0.0,
    valid_max=750_000.0,
    aggregation_method="weighted_mean",
    reads=_EDIFICABILITY_READS,
    applies_to=("block",),
    run_group=_EDIFICABILITY_GROUP,
)


EDIFICABILITY_BLOCK_DENSITY = IndicatorSpec(
    slug="edificability_block_density",
    name="Housing Density (buildable)",
    description=(
        "Buildable dwellings per hectare: housing_units / block area in "
        "hectares. Distinct from urban_form_block_building_density, which "
        "counts buildings that exist rather than dwellings the envelope allows."
    ),
    computation_kind="direct",
    model_family="urban_form",
    value_type="numeric",
    unit="units_per_ha",
    # Hong Kong's densest residential blocks approach 1,200 units/ha; 2,000 is
    # the same style of pathological-input cap used by building_density.
    #
    # Unaffected by the block-size correction that moved the other two bounds:
    # density reduces to num_levels × 10_000 / unit_size, with area cancelling
    # out entirely, so it cannot exceed 1,000 at the seeded 6 levels and 60 m²
    # no matter how large the block. Measured maximum on real Tlalpan data is
    # exactly 1,000.
    valid_min=0.0,
    valid_max=2_000.0,
    aggregation_method="weighted_mean",
    reads=_EDIFICABILITY_READS,
    applies_to=("block",),
    run_group=_EDIFICABILITY_GROUP,
)


# ── Accessibility ───────────────────────────────────────────────────────────
#
# The collaborators' brief splits accessibility in two: distance to POIs, then
# "similar to the previous point, but with an added step: updating the road
# network first affects accessibility downstream". Two indicator families, not
# one — and their cost profiles differ by orders of magnitude, which is what
# makes `resource_class` a real distinction rather than a hypothetical one.
#
#   *_euclid_m   straight-line metres, PostGIS KNN over an index. Milliseconds.
#                Invalidated by `poi_version` alone.
#   *_network_m  pedestrian-network metres, one multi-source Dijkstra per
#                category over the pgRouting tables. Seconds to minutes.
#                Invalidated by `poi_version` OR `network_version`.
#
# ⚠ `poi_version` appears in BOTH groups, `network_version` in only one. So a
# POI edit fires both families (six slugs, two dispatched units of very
# different cost) while a road edit fires only the expensive one. That
# asymmetry is the attribute→run mapping made observable, and it falls out of
# the declarations rather than being staged for a demo.

#: Distance reported when nothing of a category is reachable. Also the search
#: cutoff, so "not found" and "exactly at the limit" are the same number —
#: deliberate: both mean "no useful access", and a NULL would need a separate
#: rendering path in every consumer.
ACCESSIBILITY_CUTOFF_M = 1500.0

_POI_GROUP = "poi_access"
_NETWORK_GROUP = "network_access"

#: Amenity categories the demo reports, and the source `category` values each
#: matches. The bare English keys serve synthetic test fixtures; the rest are
#: the values actually present in the SIGCDMX Tlalpan layer, read off the
#: ingested data rather than guessed.
#
# ⚠ The source vocabulary is inconsistent and this mapping absorbs that rather
# than hiding it. `Consultorio medico` (singular, space) and
# `Consultorios_medicos` (plural, underscore) are the same thing spelled twice;
# so are `Hospital` and `Hospital_general`. Accents are stripped and spaces are
# underscores — except in `Consultorio medico`, which has a space. An earlier
# version of this table guessed the accented, space-separated forms
# ("Educación primaria", "Parques recreativos") and matched **nothing**: of
# 1,785 real amenities only `Farmacia` was found, so school and park
# accessibility would have reported the cutoff everywhere while looking
# perfectly healthy. Verify against `SELECT DISTINCT attributes->>'category'`
# before trusting this on a new layer.
ACCESSIBILITY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "school": (
        "school",
        "Guarderia",
        "Educacion_preescolar",
        "Educacion_primaria",
        "Educacion_secundaria",
        "Educacion_media_superior",
        "Educacion_superior",
    ),
    "health": (
        "health",
        "Farmacia",
        "Consultorio medico",
        "Consultorios_medicos",
        "Hospital",
        "Hospital_general",
    ),
    "park": (
        "park",
        "Parques_recreativos",
        "Clubs_deportivos_y_acondicionamiento_fisico",
        "Otros_servicios_recreativos",
    ),
}


def _accessibility_spec(category: str, *, network: bool) -> IndicatorSpec:
    """One accessibility spec. Six of these differ only by category and mode."""
    mode = "network" if network else "euclid"
    return IndicatorSpec(
        slug=f"accessibility_block_{category}_{mode}_m",
        name=f"Distance to {category} ({'network' if network else 'straight-line'})",
        description=(
            f"Metres from the block's centroid to the nearest {category}, "
            + (
                "along the OSM pedestrian network via pgRouting. Reflects "
                "actual walkable routes, so it responds to road-network edits."
                if network
                else "in a straight line. Cheap lower bound on the network "
                "distance; ignores barriers, so it is optimistic by design."
            )
            + f" Capped at {ACCESSIBILITY_CUTOFF_M:.0f} m, which is also what "
            "an unreachable block reports."
        ),
        computation_kind="direct",
        model_family="accessibility",
        value_type="numeric",
        unit="m",
        valid_min=0.0,
        valid_max=ACCESSIBILITY_CUTOFF_M,
        aggregation_method="weighted_mean",
        reads=("poi_version", "network_version") if network else ("poi_version",),
        applies_to=("block",),
        run_group=_NETWORK_GROUP if network else _POI_GROUP,
        resource_class="heavy" if network else "default",
    )


ACCESSIBILITY_EUCLID: tuple[IndicatorSpec, ...] = tuple(
    _accessibility_spec(category, network=False)
    for category in ACCESSIBILITY_CATEGORIES
)
ACCESSIBILITY_NETWORK: tuple[IndicatorSpec, ...] = tuple(
    _accessibility_spec(category, network=True) for category in ACCESSIBILITY_CATEGORIES
)

#: slug -> the source `category` values it matches.
#
# Read off the two spec tuples rather than rebuilding them: both are
# comprehensions over ACCESSIBILITY_CATEGORIES, so they share its order by
# construction and zip lines each category up with its own pair.
ACCESSIBILITY_SPEC_CATEGORY: dict[str, tuple[str, ...]] = {
    spec.slug: ACCESSIBILITY_CATEGORIES[category]
    for category, pair in zip(
        ACCESSIBILITY_CATEGORIES,
        zip(ACCESSIBILITY_EUCLID, ACCESSIBILITY_NETWORK, strict=True),
        strict=True,
    )
    for spec in pair
}


# Indicators Layer 1 / Layer 2 ships with. Order matters: dependencies must
# be upserted before the composite indicators that reference them.
#
# ⚠ New composites go at the END. tests/unit/test_failed_job_run_provenance.py
# selects the *first* spec with `model_family is None`, so inserting one earlier
# silently changes which indicator that test exercises.
SHIPPED_INDICATORS: tuple[IndicatorSpec, ...] = (
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
    URBAN_FORM_BLOCK_COMPOSITE,
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    EDIFICABILITY_BLOCK_DENSITY,
    *ACCESSIBILITY_EUCLID,
    *ACCESSIBILITY_NETWORK,
)


def seed_catalog(
    conn, specs: tuple[IndicatorSpec, ...] = SHIPPED_INDICATORS
) -> list[dict[str, Any]]:
    """Idempotently register every IndicatorSpec in `specs`.

    Returns a list of {slug, id} rows, in input order, so callers can map
    slugs back to catalog ids without a second round-trip.

    ⚠ Must run inside a single transaction, not on an autocommit connection.
    A composite's dependency rows are written one at a time and validated by a
    DEFERRABLE INITIALLY DEFERRED constraint trigger that checks the weights sum
    to 1.0 (``r016_indicator_layer``). In one transaction the trigger fires at
    COMMIT with every row present; under autocommit each INSERT commits alone,
    so the first row of a 0.5/0.5 composite fails the check by itself with
    ``indicator_dependency: weights must sum to 1.0 (got 0.5)``. ``db.connect``
    is not autocommit, so the production seed paths are fine.
    """
    backend = backend_for_connection(conn)
    repo = backend.indicator_catalog_repo()
    result: list[dict[str, Any]] = []
    for spec in specs:
        indicator_id = repo.upsert_indicator(
            slug=spec.slug,
            name=spec.name,
            computation_kind=spec.computation_kind,
            value_type=spec.value_type,
            aggregation_method=spec.aggregation_method,
            applies_to=list(spec.applies_to),
            description=spec.description,
            model_family=spec.model_family,
            unit=spec.unit,
            valid_min=spec.valid_min,
            valid_max=spec.valid_max,
        )
        if spec.depends_on:
            repo.set_dependencies(
                indicator_slug=spec.slug,
                depends_on=list(spec.depends_on),
            )
        result.append({"slug": spec.slug, "id": indicator_id})
    return result
