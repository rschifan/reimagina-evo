"""Catalog seeds for every indicator shipped with the platform.

One row per indicator. Adding a new indicator to the catalog is always just
a call to `upsert_indicator` here — no migration, no table change. The catalog
is then read by workers, the API, and UI components that need
unit / bounds / aggregation-method metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reimagina.assets.keys import AssetEdge, indicator_asset_key
from reimagina.data import amenities as amen
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
    # What this indicator consumes, as typed edges in the asset graph: the
    # datasets, the attributes read off them, and the other indicators it
    # combines. This is the declaration; `reads` and `depends_on` below are
    # derived from it, so a dataset dependency and an attribute dependency
    # cannot drift apart as two parallel statements about the same indicator.
    inputs: tuple[AssetEdge, ...] = field(default_factory=tuple)
    # Input attribute keys this indicator reads off a feature. The impact
    # engine (reimagina.orchestration.impact) seeds the dirty-set from this:
    # a scenario that edits attribute X invalidates every indicator whose
    # `reads` contains X. Composite indicators read other indicators, not
    # attributes, so they leave this empty and declare `depends_on` instead.
    #
    # ⚠ Derived from `inputs` when `inputs` is given, and only settable directly
    # when it is not. Synthetic registries — the perf workloads and the unit-test
    # fixtures for wave/port/intake — build minimal specs with no dataset
    # meaning, so they keep declaring these two directly. Every shipped
    # indicator declares `inputs` and lets the projection do the rest.
    reads: tuple[str, ...] = field(default_factory=tuple)
    depends_on: tuple[tuple[str, float | None], ...] = field(default_factory=tuple)
    # Which end of the range is the good end. Read only by `compute_composite`,
    # which flips a `False` input to `1 - normalised` so every contribution
    # points the same way before weighting.
    #
    # Needed because `indicator_dependency` enforces `weight >= 0` (r016:93), so
    # a lower-is-better input cannot be folded into a score with a negative
    # weight. Without this, a composite over an accessibility *distance* would
    # rank "far from a school" as good — silently, since the arithmetic is
    # sound and only the meaning is inverted.
    #
    # Deliberately Python-side and not a catalog column: the only consumer is
    # the composite, which already reads these specs for bounds. Add the column
    # when a reader outside this process needs it — a legend that wants to
    # reverse its ramp is the obvious first one.
    #
    # Unlike `reads`, this cannot drift from the compute: it states what the
    # number *means*, not what the code touches. A distance is lower-is-better
    # whatever the implementation does.
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if not self.inputs:
            return
        derived_reads, derived_depends = self._projections()
        # Tolerate being handed back exactly what this would derive. That is what
        # `dataclasses.replace` does — it passes every current field, including
        # the two this computes — and refusing it would break an ordinary
        # dataclass operation. Anything *else* is a second, drifting statement.
        conflicting = (self.reads and self.reads != derived_reads) or (
            self.depends_on and self.depends_on != derived_depends
        )
        if conflicting:
            raise ValueError(
                f"{self.slug}: declare `inputs`, or `reads`/`depends_on` — "
                "never both. The point of `inputs` is that there is one "
                "statement of what this indicator consumes."
            )
        object.__setattr__(self, "reads", derived_reads)
        object.__setattr__(self, "depends_on", derived_depends)

    def _projections(
        self,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, float | None], ...]]:
        attributes: list[str] = []
        for edge in self.inputs:
            for key in edge.attributes:
                if key not in attributes:
                    attributes.append(key)
        return (
            tuple(attributes),
            tuple(
                (edge.indicator_slug, edge.weight)
                for edge in self.inputs
                if edge.relation == "derived_from" and edge.indicator_slug
            ),
        )


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
    # geometry edit invalidates it — and it counts buildings, which is the
    # dataset dependency that was previously invisible outside the compute body.
    inputs=(
        AssetEdge("mex.block", attributes=("geometry",)),
        AssetEdge("global.building", relation="child_in_parent"),
    ),
    applies_to=("block",),
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
    # edit invalidates it. One dataset, one attribute — which is what makes it
    # the control case against building density in every dirty-set test.
    inputs=(AssetEdge("mex.block", attributes=("geometry",)),),
    # Indicator is produced directly at the block grain and rolled up to AGEB
    # via feature_overlap. Both grains appear in applies_to so indicator_result
    # rows land cleanly at either level.
    applies_to=("block",),
)


URBAN_FORM_BLOCK_PLANNED_FLOORS = IndicatorSpec(
    slug="urban_form_block_planned_floors",
    name="Planned Floors",
    description=(
        "The number of floors planned on a block: the base value, unless a "
        "scenario overrides the `floors` attribute. An upzoning what-if "
        "recomputes it against the override while the baseline keeps the base "
        "data, so the two are comparable side by side."
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
    inputs=(AssetEdge("mex.block", attributes=("floors",)),),
    applies_to=("block",),
)


URBAN_FORM_BLOCK_COMPOSITE = IndicatorSpec(
    slug="urban_form_block_composite",
    name="Urban Form Composite",
    description=(
        "Equal-weighted blend of block compactness (0.5) and building density "
        "(0.5), each first rescaled to 0–1 against its own declared bounds, so "
        "the two contribute equally as the weights say. 0 is least compact and "
        "least dense; 1 is most compact and most dense at the catalog's "
        "declared maxima. A relative score for ranking blocks against each "
        "other, not an absolute measure of anything."
    ),
    # ⚠ It also earns its place structurally: without a composite the dirty-set
    # is always a single wave, so nothing exercises dependency ordering,
    # downstream propagation, or the skip-a-dependent-of-a-failed-input rule.
    # That is a note to ourselves — it used to be in `description`, which the
    # API serves.
    computation_kind="composite",
    # Composites must have model_family NULL — indicator_catalog CHECK (r016).
    value_type="numeric",
    # 0–1 because `compute_composite` rescales each input against its own
    # declared bounds before weighting. Previously 0–300, when raw values were
    # summed: a 0–1 ratio added to a 0–500/ha density, which made the declared
    # 0.5/0.5 behave as 1.25 / 98.75 on real data. The weights were decorative
    # and the widest range decided. Changing this bound is what makes r016's
    # write trigger reject any pre-normalisation value that survives.
    unit="score_0_1",
    valid_min=0.0,
    valid_max=1.0,
    aggregation_method="weighted_mean",
    # Reads no attribute: a composite is invalidated through `depends_on`, not
    # by the impact engine's attribute seeding.
    inputs=(
        AssetEdge(
            indicator_asset_key(URBAN_FORM_BLOCK_COMPACTNESS.slug),
            relation="derived_from",
            weight=0.5,
        ),
        AssetEdge(
            indicator_asset_key(URBAN_FORM_BLOCK_BUILDING_DENSITY.slug),
            relation="derived_from",
            weight=0.5,
        ),
    ),
    applies_to=("block",),
)


# ── Edificability ───────────────────────────────────────────────────────────
#
# Buildable capacity from construction and land-use attributes: the worked
# example the Reimagina collaborators supplied. Three outputs of one three-line
# calculation.
#
# ⚠ All three declare the SAME `reads`, including `people_per_household`, which
# only `population` actually consumes. This was required when the three shared a
# dispatch group — identical inputs were what stopped members splitting across
# waves. That constraint is gone, so the shared tuple is now an
# over-approximation kept by choice rather than by necessity: editing
# `people_per_household` also recomputes housing_units and density. Never wrong,
# the same trade `resolve_change_set` already makes, and narrowing it is a
# behaviour change to make deliberately rather than as a side effect.
#
# ⚠ None of them reads `geometry`, though all three divide by block area. A
# scenario cannot edit block geometry today, and declaring it would drag these
# into every geometry-seeded dirty set — including the conformance fixture's,
# whose assertion that all non-poison units end `skipped` is meant to read
# identically on both ops-plane branches.

_EDIFICABILITY_READS = ("num_levels", "unit_size", "people_per_household")

#: One block edge carrying all three attributes. `geometry` stays off it for the
#: reason above: these divide by block area, but declaring it would drag them
#: into every geometry-seeded dirty set, including the conformance fixture's.
_EDIFICABILITY_INPUTS = (AssetEdge("mex.block", attributes=_EDIFICABILITY_READS),)


EDIFICABILITY_BLOCK_HOUSING_UNITS = IndicatorSpec(
    slug="edificability_block_housing_units",
    name="Housing Units (potential)",
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
    inputs=_EDIFICABILITY_INPUTS,
    applies_to=("block",),
)


EDIFICABILITY_BLOCK_POPULATION = IndicatorSpec(
    slug="edificability_block_population",
    name="Resident Population (potential)",
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
    inputs=_EDIFICABILITY_INPUTS,
    applies_to=("block",),
)


EDIFICABILITY_BLOCK_DENSITY = IndicatorSpec(
    slug="edificability_block_density",
    name="Housing Density (potential)",
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
    inputs=_EDIFICABILITY_INPUTS,
    applies_to=("block",),
)


# ── Accessibility ───────────────────────────────────────────────────────────
#
# The collaborators' brief splits accessibility in two: distance to POIs, then
# "similar to the previous point, but with an added step: updating the road
# network first affects accessibility downstream". Two indicator families, not
# one, with genuinely different costs.
#
#   *_euclid_m   straight-line metres, PostGIS KNN per block.
#                Invalidated by `poi_version` alone.
#   *_network_m  pedestrian-network metres, one multi-source Dijkstra per
#                category over the pgRouting tables.
#                Invalidated by `poi_version` OR `network_version`.
#
# ⚠ **The straight-line family is the expensive one.** This is the reverse of
# what the code asserted until it was measured, and the reverse of what the
# names suggest. Measured with `scripts/bench_real_data.py` on real Tlalpan
# data, median of 3, per indicator:
#
#             1,500 blocks          5,273 blocks (group wall-clock)
#   euclid    1455 / 868 / 662 ms   ~11.2 s
#   network    943 / 656 / 523 ms    ~4.7 s
#
# The cause is the algorithm, not the geometry. The multi-source Dijkstra runs
# **once per category** and amortizes across every block; the KNN runs **once
# per block** and does not. So euclid is linear in block count while network is
# flat, and the gap widens with scale — 1.5x at 1,500 blocks, 2.4x at 5,273.
# Euclid cost also tracks amenity count (health 1,164 > school 408 > park 203),
# the same effect seen from the other side.
#
# These numbers are kept because the *algorithmic* fact is durable and
# counter-intuitive. A `resource_class` label encoding it used to live on
# `IndicatorSpec`; it was removed because nothing ever consumed it, and because
# a hand-written cost label is exactly the kind of constant that goes stale —
# this one was assigned backwards from intuition until measured. If cost-aware
# scheduling is ever built, derive it from recorded `job_unit` durations rather
# than reintroducing a label. Re-measure with `scripts/bench_real_data.py`.
#
# ⚠ `poi_version` appears in BOTH groups, `network_version` in only one. So a
# POI edit fires both families (six slugs, two dispatched units of different
# cost) while a road edit fires only the cheaper one. That asymmetry is the
# attribute→run mapping made observable, and it falls out of the declarations
# rather than being staged for a demo.

#: Distance reported when nothing of a category is reachable. Also the search
#: cutoff, so "not found" and "exactly at the limit" are the same number —
#: deliberate: both mean "no useful access", and a NULL would need a separate
#: rendering path in every consumer.
ACCESSIBILITY_CUTOFF_M = 1500.0


def _merge(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Concatenate category tuples, dropping repeats but keeping first order.

    The source-vocabulary entries and the Overture entries collide on the bare
    English keys (``park`` is both a synthetic fixture value and a real Overture
    category), and a duplicate would reach SQL as a repeated element of the
    ``= ANY(...)`` array. Harmless, but it makes the catalog read as though the
    mapping were assembled carelessly.
    """
    seen: dict[str, None] = {}
    for group in groups:
        for value in group:
            seen.setdefault(value, None)
    return tuple(seen)


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
    "park": _merge(
        (
            "park",
            "Parques_recreativos",
            "Clubs_deportivos_y_acondicionamiento_fisico",
            "Otros_servicios_recreativos",
        ),
        amen.OVERTURE_PARK_CATEGORIES,
    ),
    # ⚠ The IMIP half of this is ``Cine`` alone — 10 cinemas. The category
    # exists because those 10 points were being ingested as establishments that
    # no indicator read, and because folding them into ``park`` to make that
    # layer non-flat would have manufactured a finding: a cinema is not open
    # space. On the Mexicali pilot it is Overture that makes the category
    # viable, at 118 destinations.
    "culture": _merge(("culture", "Cine"), amen.OVERTURE_CULTURE_CATEGORIES),
}


#: Human-readable label and the plain-language scope of each category.
#
# ⚠ These exist because the keys above are **narrower than what they match**,
# and the catalog description is what a planner reads before trusting a number.
# "park" includes private gyms; "health" includes pharmacies; "school" spans
# daycare through university. A description saying "the nearest park" while the
# SQL also accepts `Clubs_deportivos_y_acondicionamiento_fisico` is not a
# wording problem — it is the indicator claiming to measure something it does
# not. Generated into the description rather than written beside it, so the two
# cannot drift.
ACCESSIBILITY_LABELS: dict[str, tuple[str, str]] = {
    "school": (
        "school",
        "any education facility from daycare (guarderia) through university "
        "(educacion superior) — it does not distinguish school level",
    ),
    "health": (
        "health facility",
        "pharmacies, doctors' offices and hospitals alike — a farmacia counts "
        "the same as a hospital general",
    ),
    "park": (
        "park or recreation site",
        "public recreational parks, plazas, playgrounds and sports facilities, "
        "and private sports and fitness clubs — on the Mexicali pilot 632 of "
        "1,001 destinations are commercial gyms and fitness studios, so this "
        "is not a measure of public open space alone",
    ),
    "culture": (
        "cultural or entertainment venue",
        "cinemas, theatres, museums, galleries, libraries, cultural centres "
        "and arcades — it excludes music venues, which upstream are mostly "
        "bars, and historic markers, which are not places to go",
    ),
}


def _accessibility_spec(category: str) -> IndicatorSpec:
    """One accessibility spec. Three of these differ only by category.

    The straight-line (`*_euclid_m`) family was removed: it was a lower bound on
    the network distance that ignored barriers, and it did not respond to a
    road-network edit — so it answered a question no consumer asked while
    doubling the accessibility catalog. `livability_block_score` always consumed
    the network family, which is why removing it costs nothing downstream.
    """
    label, scope = ACCESSIBILITY_LABELS[category]
    return IndicatorSpec(
        slug=f"accessibility_block_{category}_network_m",
        name=f"Distance to {label} (network)",
        description=(
            f"Metres from the block's centroid to the nearest {label}, "
            "along the OSM pedestrian network via pgRouting. Reflects actual "
            "walkable routes, so it responds to road-network edits."
            f" Counts {scope}."
            f" Capped at {ACCESSIBILITY_CUTOFF_M:.0f} m, which is also what "
            "an unreachable block reports."
        ),
        computation_kind="direct",
        model_family="accessibility",
        value_type="numeric",
        unit="m",
        valid_min=0.0,
        valid_max=ACCESSIBILITY_CUTOFF_M,
        # Metres to the nearest amenity: nearer is better. All three of these
        # are the reason `higher_is_better` exists.
        higher_is_better=False,
        aggregation_method="weighted_mean",
        inputs=(
            # ⚠ `poi_version` / `network_version` are stand-in version markers
            # written onto block features by scripts/seed_demo_attributes.py and
            # bumped by hand — nothing in the ingest path produces them. They
            # stay declared here, on the block edge where they actually live, so
            # that scripts/demo.py keeps invalidating these indicators until
            # freshness replaces attribute-seeding. The real dataset edges below
            # are what will carry that job.
            AssetEdge("mex.block", attributes=("poi_version", "network_version")),
            AssetEdge(
                "global.poi", relation="within_radius", radius_m=ACCESSIBILITY_CUTOFF_M
            ),
            AssetEdge(
                "global.road_network",
                relation="within_radius",
                radius_m=ACCESSIBILITY_CUTOFF_M,
            ),
        ),
        applies_to=("block",),
    )


ACCESSIBILITY_NETWORK: tuple[IndicatorSpec, ...] = tuple(
    _accessibility_spec(category) for category in ACCESSIBILITY_CATEGORIES
)

#: slug -> the ACCESSIBILITY_CATEGORIES key it was generated from.
#
# The inverse of the comprehension that built the family. Exists so a consumer
# can select categories by name instead of by string-matching the slug, which
# is how `LIVABILITY_ACCESSIBILITY_CATEGORIES` stays readable.
ACCESSIBILITY_SPEC_CATEGORY_KEY: dict[str, str] = {
    spec.slug: category
    for category, spec in zip(
        ACCESSIBILITY_CATEGORIES, ACCESSIBILITY_NETWORK, strict=True
    )
}

#: slug -> the source `category` values it matches.
#
# Read off the spec tuple rather than rebuilding it: both are comprehensions
# over ACCESSIBILITY_CATEGORIES, so they share its order by construction.
ACCESSIBILITY_SPEC_CATEGORY: dict[str, tuple[str, ...]] = {
    spec.slug: ACCESSIBILITY_CATEGORIES[category]
    for category, spec in zip(
        ACCESSIBILITY_CATEGORIES, ACCESSIBILITY_NETWORK, strict=True
    )
}


# ── Livability score ────────────────────────────────────────────────────────
#
# The fourth worked example the Reimagina collaborators asked for: "a composite
# that combines density and accessibility indicators into a score indicator".
#
# It needs no compute code. `default_runners()` binds every
# `computation_kind="composite"` spec to the generic `compute_composite`, so a
# score is a catalog entry — which is the extensibility claim this repo makes,
# stated as a working example rather than as prose.
#
# **Network accessibility, not straight-line.** Both families exist and either
# would compose, but only the network one responds to a road-network edit. Using
# it makes the chain end to end: bump `network_version` -> three
# `*_network_m` indicators recompute -> this score recomputes behind them. That
# is the attribute -> run -> indicator mapping the collaborators asked to see,
# demonstrated on the expensive path rather than the cheap one.
#
# **The weights are a choice, and an arbitrary one.** 0.4 to density, 0.2 to
# each of three amenity classes. No literature backs that split; it is declared
# here so it can be argued with, and changing it is a one-line edit that
# restages the score through `params_digest`. What the platform guarantees is
# not that these weights are right but that they are *applied* — each input is
# rescaled to 0-1 against its declared bounds before weighting, so a 0-2000
# units/ha density cannot drown three 0-1500 m distances the way raw summing
# would.
#
# ⚠ The three distance inputs are `higher_is_better=False`, so `compute_composite`
# folds them in as `1 - normalised`. Without that a block far from every school,
# clinic and park would score *well* — `indicator_dependency` enforces
# `weight >= 0` (r016:93), so it cannot be expressed as a negative weight.
#: The accessibility categories :data:`LIVABILITY_BLOCK_SCORE` consumes.
#
# ⚠ Named explicitly, and **not** "every category in ACCESSIBILITY_NETWORK".
# The edges used to be built by iterating the whole family at a hardcoded
# ``weight=0.2``, which summed to 1.0 only because the family happened to have
# three members. Adding ``culture`` took the sum to 1.2 on an indicator declared
# ``valid_max=1.0`` — caught by ``test_the_shipped_score_mixes_both_directions``,
# which is the only reason it is not a shipped bug.
#
# ``culture`` is deliberately absent. Livability here is access to everyday
# basics — a school, a clinic, somewhere green — and a cinema is not one of
# those. The practical argument is stronger: the composite's values are
# recorded in the delivery, so quietly redefining what it measures would
# invalidate numbers already signed off, for an indicator whose Mexicali
# evidence is 118 destinations. Adding it is a deliberate change to a delivered
# definition, not a consequence of adding a category.
LIVABILITY_ACCESSIBILITY_CATEGORIES: tuple[str, ...] = ("school", "health", "park")

#: Density takes 0.4; the accessibility inputs share the remaining 0.6.
#
# Derived rather than written as 0.2 so the weights sum to 1.0 whatever
# :data:`LIVABILITY_ACCESSIBILITY_CATEGORIES` holds. The literal is what broke.
_LIVABILITY_DENSITY_WEIGHT = 0.4
# Rounded because the bare division yields 0.19999999999999998, and this value
# is persisted to `indicator_catalog.inputs`. The stored weight should read 0.2
# where 0.2 is meant; the noise would otherwise show up in every diff of the
# seeded catalog and in the asset graph.
_LIVABILITY_ACCESSIBILITY_WEIGHT = round(
    (1.0 - _LIVABILITY_DENSITY_WEIGHT) / len(LIVABILITY_ACCESSIBILITY_CATEGORIES), 10
)


LIVABILITY_BLOCK_SCORE = IndicatorSpec(
    slug="livability_block_score",
    name="Livability Score",
    description=(
        "Blend of housing density and walking distance to schools, health "
        "facilities and parks, on a 0-1 scale where 1 is the densest and "
        "best-served block. Density contributes 40%, each amenity class 20%. "
        "Each input is first rescaled against its own declared bounds, and the "
        "three distances are inverted so nearer scores higher. A relative "
        "ranking of blocks against each other, not an absolute measure of "
        "livability — the weights are a stated choice, not a finding."
    ),
    computation_kind="composite",
    # Composites must have model_family NULL — indicator_catalog CHECK (r016).
    value_type="numeric",
    unit="score_0_1",
    valid_min=0.0,
    valid_max=1.0,
    aggregation_method="weighted_mean",
    inputs=(
        AssetEdge(
            indicator_asset_key(EDIFICABILITY_BLOCK_DENSITY.slug),
            relation="derived_from",
            weight=_LIVABILITY_DENSITY_WEIGHT,
        ),
        *(
            AssetEdge(
                indicator_asset_key(spec.slug),
                relation="derived_from",
                weight=_LIVABILITY_ACCESSIBILITY_WEIGHT,
            )
            for spec in ACCESSIBILITY_NETWORK
            if ACCESSIBILITY_SPEC_CATEGORY_KEY[spec.slug]
            in LIVABILITY_ACCESSIBILITY_CATEGORIES
        ),
    ),
    applies_to=("block",),
)


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
    *ACCESSIBILITY_NETWORK,
    # Last: see the warning above about the first model_family-NULL spec.
    LIVABILITY_BLOCK_SCORE,
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
