"""End-to-end: a composite indicator computed through the full orchestrator.

Exercises the composite path — dirty-set downstream propagation, coordinator
dependency ordering, the DB acyclicity/weights trigger, and generic
compute_composite reading its inputs' just-written results — with the two
shipped indicators as inputs.
"""

from __future__ import annotations

import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    ACCESSIBILITY_CUTOFF_M,
    SHIPPED_INDICATORS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    IndicatorSpec,
    seed_catalog,
)
from reimagina.indicators.composite import compute_composite
from reimagina.jobs._lifecycle import job_run
from reimagina.orchestration.synchronous import SynchronousOrchestrator
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

_COMPOSITE = IndicatorSpec(
    slug="urban_form_block_composite",
    name="Urban Form Composite",
    computation_kind="composite",
    value_type="numeric",
    aggregation_method="weighted_mean",
    applies_to=("block",),
    valid_min=0.0,
    valid_max=300.0,
    # composites must have model_family NULL (indicator_catalog CHECK)
    depends_on=(
        (URBAN_FORM_BLOCK_COMPACTNESS.slug, 0.5),
        (URBAN_FORM_BLOCK_BUILDING_DENSITY.slug, 0.5),
    ),
)
_REGISTRY = (
    URBAN_FORM_BLOCK_COMPACTNESS,
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    _COMPOSITE,
)
_BLOCK_WKT = (
    "POLYGON((-99.140 19.430,-99.137 19.430,-99.137 19.433,"
    "-99.140 19.433,-99.140 19.430))"
)


def test_composite_runs_after_inputs_and_combines_them(conn):
    seed_catalog(conn, _REGISTRY)  # registers the composite + its dependency edges
    source_id = _register_source(
        conn,
        name="composite_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_id = _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="comp-blk-1",
        geometry_wkt=_BLOCK_WKT,
    )

    # A real building, so the composite combines two measured halves. With the
    # building layer empty, density was 0.0 and the composite collapsed to
    # exactly 0.5 x compactness -- which is what shipped to production and went
    # unnoticed, so a test asserting "combines them" must not reproduce it.
    _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "global", "building"),
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref="comp-bld-1",
        geometry_wkt=(
            "POLYGON((-99.1395 19.4305,-99.1390 19.4305,-99.1390 19.4310,"
            "-99.1395 19.4310,-99.1395 19.4305))"
        ),
    )

    report = SynchronousOrchestrator(registry=_REGISTRY).submit_run(
        conn,
        changed_attributes={"geometry"},
        edited_features={block_id},
        code_version="test",
    )
    assert report.status is not None
    assert report.status.status == "succeeded"
    assert {u.indicator for u in report.status.units} == {
        URBAN_FORM_BLOCK_COMPACTNESS.slug,
        URBAN_FORM_BLOCK_BUILDING_DENSITY.slug,
        _COMPOSITE.slug,
    }

    repo = backend_for_connection(conn).indicator_result_repo()
    compactness = repo.get_current(
        feature_id=block_id, indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug
    )
    density = repo.get_current(
        feature_id=block_id, indicator_slug=URBAN_FORM_BLOCK_BUILDING_DENSITY.slug
    )
    composite = repo.get_current(feature_id=block_id, indicator_slug=_COMPOSITE.slug)
    assert composite is not None

    # Composite = 0.5·norm(compactness) + 0.5·norm(density), where norm rescales
    # each input against its own declared bounds. Asserted from the specs rather
    # than a literal, so widening a bound moves the expectation with it.
    def _norm(spec, value: float) -> float:
        return (value - spec.valid_min) / (spec.valid_max - spec.valid_min)

    expected = 0.5 * _norm(
        URBAN_FORM_BLOCK_COMPACTNESS, compactness["value_numeric"]
    ) + 0.5 * _norm(URBAN_FORM_BLOCK_BUILDING_DENSITY, density["value_numeric"])
    assert composite["value_numeric"] == pytest.approx(expected)

    # And the thing the raw sum got wrong: the result stays inside 0-1, so the
    # declared weights bound each input's contribution instead of its range
    # doing it. Before normalisation this value could reach ~250.
    assert 0.0 <= composite["value_numeric"] <= 1.0


# ── direction, through the real compute path ─────────────────────────────

_DISTANCE = "accessibility_block_school_network_m"
#: The shipped spec, not a copy — its bounds and `higher_is_better` are
#: what compute_composite reads, so re-declaring them here could disagree.
_DISTANCE_SPEC = next(s for s in SHIPPED_INDICATORS if s.slug == _DISTANCE)

_SCORE = IndicatorSpec(
    slug="livability_block_score",
    name="Livability Score (test)",
    computation_kind="composite",
    value_type="numeric",
    aggregation_method="weighted_mean",
    applies_to=("block",),
    valid_min=0.0,
    valid_max=1.0,
    depends_on=((URBAN_FORM_BLOCK_COMPACTNESS.slug, 0.5), (_DISTANCE, 0.5)),
)


def test_a_score_mixing_a_distance_ranks_nearer_blocks_higher(conn):
    """The shape Uriel asked for: density/form combined with accessibility.

    Exercised through `compute_composite` rather than a helper mirroring it,
    because the whole risk here is that the real path forgets to orient. With
    `weight >= 0` enforced by r016, an unoriented distance makes 'far from a
    school' the high score — arithmetically sound, semantically inverted.
    """
    registry = (URBAN_FORM_BLOCK_COMPACTNESS, _DISTANCE_SPEC, _SCORE)
    seed_catalog(conn, registry)
    source_id = _register_source(
        conn,
        name="dir_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")

    def _block(ref: str, wkt: str) -> str:
        return _insert_feature(
            conn,
            type_id=block_type,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=wkt,
        )

    near = _block("dir-near", _BLOCK_WKT)
    far = _block(
        "dir-far",
        "POLYGON((-99.130 19.430,-99.127 19.430,-99.127 19.433,"
        "-99.130 19.433,-99.130 19.430))",
    )

    backend = backend_for_connection(conn)
    repo = backend.indicator_result_repo()
    # Identical compactness on both, so only the distance can move the score.
    with job_run(
        backend, model_family="urban_form", code_version="test", scenario_id=None
    ) as jid:
        repo.bulk_write_numeric_results(
            [(near, 0.5), (far, 0.5)],
            indicator_slug=URBAN_FORM_BLOCK_COMPACTNESS.slug,
            job_run_id=jid,
        )
    with job_run(
        backend,
        model_family="accessibility",
        code_version="test",
        scenario_id=None,
    ) as jid:
        repo.bulk_write_numeric_results(
            [(near, 100.0), (far, 1400.0)],
            indicator_slug=_DISTANCE,
            job_run_id=jid,
        )

    compute_composite(conn, spec=_SCORE, code_version="test")

    near_score = repo.get_current(feature_id=near, indicator_slug=_SCORE.slug)
    far_score = repo.get_current(feature_id=far, indicator_slug=_SCORE.slug)
    assert near_score is not None and far_score is not None
    assert near_score["value_numeric"] > far_score["value_numeric"], (
        "a block 100 m from a school must score above one 1400 m away; "
        f"got near={near_score['value_numeric']} far={far_score['value_numeric']}"
    )
    # Exact maths: 0.5·norm(compactness) + 0.5·(1 − norm(distance)).
    assert near_score["value_numeric"] == pytest.approx(
        0.5 * 0.5 + 0.5 * (1.0 - 100.0 / ACCESSIBILITY_CUTOFF_M)
    )
