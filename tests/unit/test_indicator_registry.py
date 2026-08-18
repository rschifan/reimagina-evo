"""Guards on the shipped indicator registry itself.

These are cheap invariants with expensive failure modes: a composite whose
weights do not sum to 1.0 trips a `RAISE EXCEPTION` in the indicator_dependency
trigger, and `bootstrap_local_platform` seeds the catalog, so a bad entry breaks
`make migrate` for everyone rather than failing in one test.
"""

from __future__ import annotations


import pytest

from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.orchestration.runners import default_runners

_COMPOSITES = [s for s in SHIPPED_INDICATORS if s.computation_kind == "composite"]


def test_registry_ships_at_least_one_composite():
    """Without a composite the dirty-set is always one wave.

    Dependency ordering, downstream propagation and the skip-a-dependent-of-a-
    failed-input rule then have no production exercise at all.
    """
    assert _COMPOSITES, "the registry must ship a composite to exercise the DAG"


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_weights_sum_to_one(spec):
    # indicator_dependency's trigger raises outside a 1e-9 tolerance.
    total = sum(weight or 0.0 for _dep, weight in spec.depends_on)
    assert abs(total - 1.0) < 1e-9, f"{spec.slug} weights sum to {total}, not 1.0"


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_has_no_model_family(spec):
    # indicator_catalog CHECK: composites must have model_family NULL.
    assert spec.model_family is None


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_composite_inputs_are_registered_before_it(spec):
    """seed_catalog iterates in order and set_dependencies needs parents first."""
    order = [s.slug for s in SHIPPED_INDICATORS]
    for dep_slug, _weight in spec.depends_on:
        assert dep_slug in order, f"{spec.slug} depends on unregistered {dep_slug}"
        assert order.index(dep_slug) < order.index(spec.slug)


def test_every_shipped_indicator_has_a_runner():
    """The CLI and the coordinator both dispatch through this registry."""
    runners = default_runners()
    missing = [s.slug for s in SHIPPED_INDICATORS if s.slug not in runners]
    assert not missing, f"no runner registered for: {missing}"


def test_every_accessibility_category_declares_a_label_and_scope():
    """The description must state what the category actually matches.

    ⚠ These keys are narrower than the values they match: `park` also accepts
    private sports and fitness clubs, `health` accepts pharmacies, `school`
    spans daycare through university. A description reading "the nearest park"
    while the SQL accepts a gym is the indicator claiming to measure something
    it does not, so the scope clause is generated from this table rather than
    written beside it — and a new category without one must fail here rather
    than ship an unqualified description.
    """
    from reimagina.indicators.catalog import (
        ACCESSIBILITY_CATEGORIES,
        ACCESSIBILITY_LABELS,
    )

    assert set(ACCESSIBILITY_LABELS) == set(
        ACCESSIBILITY_CATEGORIES
    ), "every accessibility category needs a (label, scope) entry"
    for category, (label, scope) in ACCESSIBILITY_LABELS.items():
        assert label and scope, f"{category} has an empty label or scope"


def test_accessibility_descriptions_state_their_scope():
    """Generated, not hand-written, so the two cannot drift apart."""
    from reimagina.indicators.catalog import ACCESSIBILITY_LABELS, SHIPPED_INDICATORS

    accessibility = [
        s for s in SHIPPED_INDICATORS if s.slug.startswith("accessibility")
    ]
    assert accessibility, "no accessibility indicators found"
    for spec in accessibility:
        category = spec.slug.split("_")[2]
        label, scope = ACCESSIBILITY_LABELS[category]
        assert spec.description is not None
        assert scope in spec.description, f"{spec.slug} omits what it counts"
        assert label in spec.name, f"{spec.name} does not use the display label"


# ── input declarations ──────────────────────────────────────────────────────
#
# `inputs` is the single statement of what an indicator consumes; `reads` and
# `depends_on` are projections of it. These guards keep the projection honest,
# because everything downstream — the impact engine, the composite runner, the
# wave scheduler — reads the projections and would not notice a bad one.


_DIRECT = [s for s in SHIPPED_INDICATORS if s.computation_kind == "direct"]


@pytest.mark.parametrize("spec", SHIPPED_INDICATORS, ids=lambda s: s.slug)
def test_every_shipped_indicator_declares_inputs(spec):
    """A spec with no inputs consumes nothing, so nothing can ever invalidate it."""
    assert spec.inputs, f"{spec.slug} declares no inputs"


@pytest.mark.parametrize("spec", _DIRECT, ids=lambda s: s.slug)
def test_a_direct_indicator_declares_at_least_one_dataset(spec):
    """`reads` alone never said which dataset an attribute lives on.

    That was the gap: `get_type_id(conn, "global", "building")` inside a compute
    body was the real dependency, invisible to the catalog and to the impact
    engine.
    """
    datasets = [e for e in spec.inputs if e.relation != "derived_from"]
    assert datasets, f"{spec.slug} declares no dataset input"


@pytest.mark.parametrize("spec", _COMPOSITES, ids=lambda s: s.slug)
def test_a_composite_declares_only_indicator_edges(spec):
    """A composite's inputs are derived: whatever its components consume.

    Declaring a dataset on it would be a second, drifting statement of something
    already implied by the closure.
    """
    datasets = [e for e in spec.inputs if e.relation != "derived_from"]
    assert not datasets, f"{spec.slug} declares dataset inputs: {datasets}"


@pytest.mark.parametrize("spec", SHIPPED_INDICATORS, ids=lambda s: s.slug)
def test_every_declared_input_resolves_to_a_registered_asset(spec):
    """Seeding raises on an unknown key; failing here names the typo first."""
    from reimagina.assets.keys import indicator_asset_key
    from reimagina.assets.registry import SHIPPED_ASSETS

    known = {a.asset_key for a in SHIPPED_ASSETS}
    known |= {indicator_asset_key(s.slug) for s in SHIPPED_INDICATORS}
    unknown = [e.asset_key for e in spec.inputs if e.asset_key not in known]
    assert not unknown, f"{spec.slug} declares unknown inputs: {unknown}"


@pytest.mark.parametrize("spec", SHIPPED_INDICATORS, ids=lambda s: s.slug)
def test_a_radius_is_declared_exactly_for_within_radius_edges(spec):
    """Mirrors the asset_dependency_radius CHECK, so a bad spec fails offline."""
    for edge in spec.inputs:
        if edge.relation == "within_radius":
            assert edge.radius_m and edge.radius_m > 0, f"{spec.slug}: {edge}"
        else:
            assert edge.radius_m is None, f"{spec.slug}: {edge}"


def test_accessibility_radius_equals_the_constant_the_sql_uses():
    """Pins the declaration to the compute.

    ACCESSIBILITY_CUTOFF_M is what `_euclid_rows` passes as `%(cutoff)s`. If the
    two ever diverge the declaration becomes decorative — which is exactly how
    `poi_version`/`network_version` rotted into tokens nothing produced.
    """
    from reimagina.indicators.catalog import ACCESSIBILITY_CUTOFF_M

    radii = {
        edge.radius_m
        for spec in SHIPPED_INDICATORS
        for edge in spec.inputs
        if edge.relation == "within_radius"
    }
    assert radii == {ACCESSIBILITY_CUTOFF_M}


def test_a_conflicting_reads_declaration_is_refused():
    """One statement or the other, never two that can drift apart.

    Passing back exactly what `inputs` derives is allowed — that is what
    `dataclasses.replace` does, and refusing it would break an ordinary
    dataclass operation. A *different* value is the drift this guards.
    """
    from reimagina.assets.keys import AssetEdge
    from reimagina.indicators.catalog import IndicatorSpec

    def _spec(**kwargs):
        return IndicatorSpec(
            slug="x",
            name="X",
            computation_kind="direct",
            model_family="urban_form",
            value_type="numeric",
            aggregation_method="mean",
            applies_to=("block",),
            inputs=(AssetEdge("mex.block", attributes=("geometry",)),),
            **kwargs,
        )

    with pytest.raises(ValueError, match="never both"):
        _spec(reads=("floors",))

    # The derived value handed straight back is not a conflict.
    assert _spec(reads=("geometry",)).reads == ("geometry",)


def test_projections_match_the_literals_they_replaced():
    """Byte-for-byte, because everything downstream still reads the projections.

    A drift here is a silent behaviour change in the dirty set: `scripts/demo.py`
    submits `poi_version` and `network_version` as edited attributes, and the
    conformance fixture seeds on `geometry`.
    """
    by_slug = {s.slug: s for s in SHIPPED_INDICATORS}
    assert by_slug["urban_form_block_compactness"].reads == ("geometry",)
    assert by_slug["urban_form_block_building_density"].reads == ("geometry",)
    assert by_slug["urban_form_block_planned_floors"].reads == ("floors",)
    assert by_slug["urban_form_block_composite"].reads == ()
    assert by_slug["urban_form_block_composite"].depends_on == (
        ("urban_form_block_compactness", 0.5),
        ("urban_form_block_building_density", 0.5),
    )
    for slug in (
        "edificability_block_housing_units",
        "edificability_block_population",
        "edificability_block_density",
    ):
        assert by_slug[slug].reads == (
            "num_levels",
            "unit_size",
            "people_per_household",
        )
    assert by_slug["accessibility_block_school_euclid_m"].reads == ("poi_version",)
    assert by_slug["accessibility_block_school_network_m"].reads == (
        "poi_version",
        "network_version",
    )


# ── config is data ──────────────────────────────────────────────────────────


def test_catalog_params_reach_the_asset_digest():
    """`valid_max` and friends live in a table, so no commit ever changes.

    They decide which rows survive validation and which arithmetic a rollup
    uses, so an edit to one has to invalidate that indicator's results — and a
    per-asset `code_version` would never notice.
    """
    from dataclasses import replace

    from reimagina.assets.registry import _params_digest

    spec = SHIPPED_INDICATORS[0]
    assert _params_digest(spec) != _params_digest(replace(spec, valid_max=99.0))
    assert _params_digest(spec) != _params_digest(
        replace(spec, aggregation_method="sum")
    )
    assert _params_digest(spec) != _params_digest(replace(spec, applies_to=("ageb",)))


def test_cosmetic_catalog_edits_do_not_move_the_digest():
    """A reworded description must not restage every result in the platform.

    The digest is built from a fixed field list rather than the whole dataclass
    for exactly this reason.
    """
    from dataclasses import replace

    from reimagina.assets.registry import _params_digest

    spec = SHIPPED_INDICATORS[0]
    assert _params_digest(spec) == _params_digest(
        replace(spec, description="reworded", name="Renamed")
    )


def test_every_shipped_indicator_has_a_params_digest():
    from reimagina.assets.registry import indicator_assets

    assert all(asset.params_digest for asset in indicator_assets())


def test_seeding_a_subset_reports_the_missing_asset(monkeypatch):
    """A partial specs list must not surface as a bare KeyError.

    seed_asset_edges always covers every shipped indicator, so indexing the id
    map directly hides a seeding mistake behind KeyError instead of the message
    written for exactly this case.
    """
    from reimagina.assets import registry

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []  # nothing registered

    monkeypatch.setattr(
        registry,
        "backend_for_connection",
        lambda conn: type("B", (), {"name": "postgres"})(),
    )
    conn = type("C", (), {"cursor": lambda self: _Cur()})()

    with pytest.raises(RuntimeError, match="has no registered asset"):
        registry.seed_asset_edges(conn)
