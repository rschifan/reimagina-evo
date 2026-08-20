"""Integration tests for serving_geo.indicator_overlay_tile (r017).

Skipped when mapbox_vector_tile isn't available — it's a dev-only
dependency. The local dev shell had it, but CI may not; the skip keeps
the main pytest run green while still exercising the function against
real PostGIS when the decoder is present.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from reimagina.data import feature as feat
from reimagina.indicators.catalog import seed_catalog
from reimagina.indicators.compactness import compute_block_compactness

try:
    import mapbox_vector_tile  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    mapbox_vector_tile = None


pytestmark = pytest.mark.skipif(
    mapbox_vector_tile is None,
    reason="mapbox_vector_tile is not installed; add it to the dev deps to run tile decode tests",
)


def _cuauhtemoc_style_polygon_wkt() -> str:
    """A four-vertex polygon positioned on a known Mexico City tile.

    Coordinates correspond to tile z=14 / x=3680 / y=7290 (Cuauhtémoc
    centre), so `tile_envelope(z=14, x=3680, y=7290)` intersects it.
    """
    return (
        "POLYGON((-99.137 19.428,-99.133 19.428,"
        "-99.133 19.432,-99.137 19.432,-99.137 19.428))"
    )


def test_tile_is_empty_without_indicator_slug(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT octet_length(serving_geo.indicator_overlay_tile(14, 3680, 7290, '{}'::json))"
        )
        size = cur.fetchone()[0]
    assert size == 0, "Tile function must return empty when indicator_slug is missing"


def test_tile_is_empty_below_minzoom(conn):
    """Choropleth is misleading at city scale; function returns empty below z=10."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT octet_length(serving_geo.indicator_overlay_tile(
              8, 56, 113,
              '{"indicator_slug": "urban_form_block_building_density"}'::json
            ))
            """
        )
        assert cur.fetchone()[0] == 0


def test_tile_decodes_with_expected_properties(conn):
    """End-to-end: seed a block + a compactness value, render the tile, decode."""
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="tile_fn_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref=f"tile-{uuid4().hex[:8]}",
        geometry_wkt=_cuauhtemoc_style_polygon_wkt(),
    )
    compute_block_compactness(conn, code_version="test")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT serving_geo.indicator_overlay_tile(14, 3680, 7290, %s::json)
            """,
            ('{"indicator_slug": "urban_form_block_compactness"}',),
        )
        tile_bytes = bytes(cur.fetchone()[0])

    assert (
        len(tile_bytes) > 0
    ), "tile must not be empty when indicator + feature are in bbox"

    decoded = mapbox_vector_tile.decode(tile_bytes)
    assert "indicator_overlay" in decoded, f"unexpected layers: {list(decoded)}"
    layer = decoded["indicator_overlay"]
    features = layer.get("features") or []
    assert features, "at least one feature expected in the rendered tile"
    properties = features[0]["properties"]
    # feature_id + value_numeric + quality_flag are the three properties
    # the frontend's fill-color expression keys off of. Their presence
    # here is the contract every indicator_overlay tile must honour.
    assert properties.get("feature_id") == block_id
    assert "value_numeric" in properties
    assert 0.0 <= float(properties["value_numeric"]) <= 1.0
    assert properties.get("quality_flag") == "measured"
    assert properties.get("type_slug") == "block"


def test_tile_filters_by_scenario_id(conn):
    """scenario_id=NULL (base) tile should NOT include a scenario-scoped row.

    Writes one base + one scenario-scoped indicator_result for the same
    feature. A base-scoped tile renders only the base row; a scenario-
    scoped tile renders only that scenario's row.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )
    from reimagina.storage import backend_for_connection

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="tile_scenario_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)
    block_type = feat.get_type_id(conn, "MEX", "block")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref=f"scn-{uuid4().hex[:8]}",
        geometry_wkt=_cuauhtemoc_style_polygon_wkt(),
    )
    compute_block_compactness(conn, code_version="test")

    backend = backend_for_connection(conn)
    job_run_id = backend.job_run_repo().start_job(
        model_family="urban_form", code_version="test", status="running"
    )
    # We can't reuse the real scenario table (no Layer 3 yet), so use a
    # random uuid as a sentinel scenario_id. The indicator_result trigger
    # accepts any uuid — only later Layer 3 adds the FK.
    scenario_id = str(uuid4())
    # Override job_run.scenario_id so the indicator_result scenario match
    # accepts the write. Direct UPDATE because we don't expose this through
    # the JobRunRepository contract.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs_runtime.job_run SET scenario_id = %s WHERE id = %s",
            (scenario_id, job_run_id),
        )
    backend.indicator_result_repo().write_result(
        feature_id=block_id,
        indicator_slug="urban_form_block_compactness",
        job_run_id=job_run_id,
        scenario_id=scenario_id,
        value_numeric=0.42,
    )

    # Base-scoped tile: feature present with its base (compactness) value,
    # NOT 0.42.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT serving_geo.indicator_overlay_tile(14, 3680, 7290, %s::json)",
            ('{"indicator_slug": "urban_form_block_compactness"}',),
        )
        base_tile = bytes(cur.fetchone()[0])
    base = mapbox_vector_tile.decode(base_tile)["indicator_overlay"]["features"]
    assert len(base) == 1
    assert (
        abs(base[0]["properties"]["value_numeric"] - 0.42) > 1e-6
    ), "base tile must not include scenario-scoped rows"

    # Scenario-scoped tile: feature present with value 0.42.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT serving_geo.indicator_overlay_tile(14, 3680, 7290, %s::json)",
            (
                f'{{"indicator_slug": "urban_form_block_compactness", '
                f'"scenario_id": "{scenario_id}"}}',
            ),
        )
        scenario_tile = bytes(cur.fetchone()[0])
    scenario = mapbox_vector_tile.decode(scenario_tile)["indicator_overlay"]["features"]
    assert len(scenario) == 1
    assert scenario[0]["properties"]["value_numeric"] == pytest.approx(0.42)


def test_tile_filters_by_feature_type(conn):
    """r018's feature_type param scopes a tile to one grain.

    ⚠ The regression it was written for can no longer arise from the shipped
    catalog. Every indicator is now `applies_to=("block",)`, and r016's write
    trigger rejects a row at any other grain — so the original setup (the same
    slug written at block *and* AGEB, rendering overlapping polygons) cannot be
    built any more. What is still worth holding is that the param filters at
    all, since the client sends it on every overlay request.

    Restore the two-grain version if a multi-grain indicator returns.
    """
    from tests.integration._helpers import (
        insert_feature as _insert_feature,
        register_source as _register_source,
        start_run as _start_run,
    )
    from reimagina.storage import backend_for_connection

    seed_catalog(conn)
    source_id = _register_source(
        conn,
        name="tile_feature_type_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = _start_run(conn, source_id)

    # Block + AGEB features, both intersecting the same z=14 tile.
    block_type = feat.get_type_id(conn, "MEX", "block")
    ageb_type = feat.get_type_id(conn, "MEX", "ageb")
    block_id = _insert_feature(
        conn,
        type_id=block_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref=f"block-{uuid4().hex[:8]}",
        geometry_wkt=_cuauhtemoc_style_polygon_wkt(),
    )
    # An AGEB feature covering the same tile. No indicator applies to it any
    # more, so it exists here only to prove the filter excludes its grain.
    _insert_feature(
        conn,
        type_id=ageb_type,
        source_id=source_id,
        ingestion_run_id=run_id,
        dataset_version="v1",
        source_record_ref=f"ageb-{uuid4().hex[:8]}",
        geometry_wkt=_cuauhtemoc_style_polygon_wkt(),
    )

    # One block row. The AGEB write that used to accompany it is now rejected
    # by r016: "feature type ageb is not in indicator applies_to ({block})".
    backend = backend_for_connection(conn)
    job_run_id = backend.job_run_repo().start_job(
        model_family="urban_form",
        code_version="test",
        status="running",
    )
    slug = "urban_form_block_building_density"
    result_repo = backend.indicator_result_repo()
    result_repo.write_result(
        feature_id=block_id,
        indicator_slug=slug,
        job_run_id=job_run_id,
        value_numeric=12.0,
    )

    def _render(params_json: str) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT serving_geo.indicator_overlay_tile(14, 3680, 7290, %s::json)",
                (params_json,),
            )
            tile_bytes = bytes(cur.fetchone()[0])
        if not tile_bytes:
            return []
        return (
            mapbox_vector_tile.decode(tile_bytes)
            .get("indicator_overlay", {})
            .get("features", [])
        )

    block_only = _render(f'{{"indicator_slug": "{slug}", "feature_type": "block"}}')
    assert len(block_only) == 1
    assert block_only[0]["properties"]["feature_id"] == block_id
    assert block_only[0]["properties"]["type_slug"] == "block"

    # Asking for a grain the indicator does not apply to returns nothing —
    # the AGEB feature is present and overlaps the tile, so this fails if the
    # param is ignored rather than because there is no candidate geometry.
    ageb_only = _render(f'{{"indicator_slug": "{slug}", "feature_type": "ageb"}}')
    assert ageb_only == []

    # No filter: the block row still lands, so r018 has not regressed into
    # "always filter by grain".
    both = _render(f'{{"indicator_slug": "{slug}"}}')
    assert {f["properties"]["type_slug"] for f in both} == {"block"}
