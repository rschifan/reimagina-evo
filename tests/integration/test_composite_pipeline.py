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
    URBAN_FORM_BLOCK_BUILDING_DENSITY,
    URBAN_FORM_BLOCK_COMPACTNESS,
    IndicatorSpec,
    seed_catalog,
)
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
    # Composite = 0.5·compactness + 0.5·density, from their just-written results.
    assert composite is not None
    assert composite["value_numeric"] == pytest.approx(
        0.5 * compactness["value_numeric"] + 0.5 * density["value_numeric"]
    )
