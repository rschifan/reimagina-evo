"""Unit tests for reimagina.pipeline.points spec coherence + structure.

DB-free. The actual stage+load behaviour is covered by the integration
suite at ``tests/integration/test_points_ingest.py``.
"""

from __future__ import annotations

from reimagina.pipeline.points import (
    DEFAULT_LOAD_ORDER,
    LAYER_SPECS,
    POINTS_FEATURE_TYPE_CONTEXT,
    POINTS_FEATURE_TYPE_SLUG,
    MEXICALI_AMENITIES,
    PointsLayerSpec,
    TLALPAN_AMENITIES,
)


def test_layer_specs_match_default_load_order():
    """If a new spec is added without listing it in DEFAULT_LOAD_ORDER,
    bootstrap() will silently skip it — almost always a bug."""
    assert set(LAYER_SPECS.keys()) == set(DEFAULT_LOAD_ORDER)


def test_layer_specs_share_common_attributes_schema():
    """All amenity specs must declare the same attribute set so
    downstream queries can rely on a uniform JSONB shape."""
    expected = {"category", "capacity", "fuente", "fecha", "entidad"}
    for spec in LAYER_SPECS.values():
        assert set(spec.attributes_cols) == expected, (
            f"spec {spec.key!r} has divergent attributes_cols"
        )


def test_layer_specs_use_stable_record_id_column():
    """source_record_ref must point at the upstream `id` column so
    re-ingest is idempotent on (source_id, source_record_ref)."""
    for spec in LAYER_SPECS.values():
        assert spec.source_record_ref_col == "id"


def test_feature_type_target_is_global_establishment():
    """Existing global:establishment POI type from r003 — no migration
    required for Tier 1.6. A future migration can introduce a more
    specific 'amenity_point' type when it becomes useful."""
    assert POINTS_FEATURE_TYPE_CONTEXT == "global"
    assert POINTS_FEATURE_TYPE_SLUG == "establishment"


def test_specs_carry_a_public_lineage_uri():
    """data_source_catalog.storage_uri is where the data comes from: a
    public publisher page, never a local or private path."""
    for spec in LAYER_SPECS.values():
        assert spec.lineage_uri.startswith("https://")


def test_pilot_specs_target_distinct_cities():
    """Tlalpan and Mexicali specs must point at different geoparquets
    and different providers — the symmetry hides bugs otherwise."""
    assert (
        TLALPAN_AMENITIES.geoparquet_relative != MEXICALI_AMENITIES.geoparquet_relative
    )
    assert TLALPAN_AMENITIES.provider != MEXICALI_AMENITIES.provider


def test_specs_are_immutable():
    """PointsLayerSpec is a frozen dataclass; mutation must raise."""
    import dataclasses

    try:
        TLALPAN_AMENITIES.version = "mutated"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("PointsLayerSpec should be frozen")


def test_dataclass_signature_supports_minimal_construction():
    """The required positional+keyword args are ``key``, ``geoparquet_relative``,
    ``lineage_uri``, ``display_name``, ``provider``, ``license``, ``version``,
    ``notes`` — anything beyond those should have defaults so adding a new
    spec stays a one-liner."""
    spec = PointsLayerSpec(
        key="x",
        geoparquet_relative="x.geoparquet",
        lineage_uri="https://example.org/x",
        display_name="X",
        provider="P",
        license="L",
        version="v",
        notes="n",
    )
    assert spec.source_record_ref_col == "id"
    assert spec.name_col == "category"
    assert "category" in spec.attributes_cols


def test_mexicali_amenities_credit_inegi_not_imip():
    """The Mexicali file is INEGI DENUE (every row's ``fuente`` is DENUE,
    ``fecha`` 2025-05), so the source must name INEGI under INEGI's terms —
    the same provider and licence as the INEGI boundaries and census. It used
    to credit "IMIP Mexicali"."""
    from reimagina.data.attribution import attribution_text
    from reimagina.pipeline.inegi_marco import (
        INEGI_MARCO_LICENSE,
        INEGI_MARCO_PROVIDER,
    )

    assert MEXICALI_AMENITIES.provider == INEGI_MARCO_PROVIDER
    assert MEXICALI_AMENITIES.license == INEGI_MARCO_LICENSE
    assert MEXICALI_AMENITIES.display_name == "Mexicali · Amenities (INEGI DENUE)"
    assert attribution_text(
        MEXICALI_AMENITIES.provider, MEXICALI_AMENITIES.license
    ) == attribution_text(INEGI_MARCO_PROVIDER, INEGI_MARCO_LICENSE)
    assert "IMIP" not in f"{MEXICALI_AMENITIES.display_name} {MEXICALI_AMENITIES.notes}"
