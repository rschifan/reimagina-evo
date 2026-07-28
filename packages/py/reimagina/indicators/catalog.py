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


# Indicators Layer 1 / Layer 2 ships with. Order matters: dependencies must
# be upserted before the composite indicators that reference them.
SHIPPED_INDICATORS: tuple[IndicatorSpec, ...] = (
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_PLANNED_FLOORS,
    URBAN_FORM_BLOCK_COMPOSITE,
)


def seed_catalog(
    conn, specs: tuple[IndicatorSpec, ...] = SHIPPED_INDICATORS
) -> list[dict[str, Any]]:
    """Idempotently register every IndicatorSpec in `specs`.

    Returns a list of {slug, id} rows, in input order, so callers can map
    slugs back to catalog ids without a second round-trip.
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
