"""The fixture seeder: merges without clobbering, and feeds the indicators.

Two properties matter and both fail silently if broken. The merge must preserve
the ingest's identifier keys — assigning over `attributes` would destroy the
CVEGEO lineage that joins blocks to census data — and the seeded values must be
the ones `effective_numeric` later resolves, or the indicators compute nothing
and report success.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from reimagina.data import feature as feat
from reimagina.indicators.catalog import (
    EDIFICABILITY_BLOCK_HOUSING_UNITS,
    EDIFICABILITY_BLOCK_POPULATION,
    seed_catalog,
)
from reimagina.indicators.edificability import (
    compute_block_housing_units,
    compute_block_population,
)
from reimagina.storage import backend_for_connection

from tests.integration._helpers import (
    insert_feature as _insert_feature,
    register_source as _register_source,
    start_run as _start_run,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from seed_demo_attributes import (  # noqa: E402
    CONSTANT_ATTRIBUTES,
    levels_for,
    seed,
)

_WKT = (
    "POLYGON((-99.140000 19.430000,-99.139859 19.430000,"
    "-99.139859 19.430134,-99.140000 19.430134,-99.140000 19.430000))"
)


def _block(conn, attributes, ref=None, wkt=_WKT):
    source_id = _register_source(
        conn,
        name=f"seed_src_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)
    return _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "MEX", "block"),
        source_id=source_id,
        ingestion_run_id=ingest,
        dataset_version="v1",
        source_record_ref=ref or f"seed-blk-{uuid4().hex[:8]}",
        geometry_wkt=wkt,
        attributes=attributes,
    )


def test_seed_merges_and_preserves_existing_attributes(conn, migrated_db):
    """`||`, not assignment: CVEGEO must survive the seed."""
    ref = f"0901500011234-{uuid4().hex[:8]}"
    block_id = _block(conn, {"CVEGEO": "0901500011234", "AMBITO": "Urbana"}, ref)
    conn.commit()

    result = seed(migrated_db, aoi_id=None, dry_run=False)
    assert result["written"] >= 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT attributes FROM curated_geo.feature WHERE id = %s::uuid",
            (block_id,),
        )
        attributes = cur.fetchone()[0]

    # Pre-existing keys survive...
    assert attributes["CVEGEO"] == "0901500011234"
    assert attributes["AMBITO"] == "Urbana"
    # ...alongside every seeded key.
    for key, value in CONSTANT_ATTRIBUTES.items():
        assert attributes[key] == value
    assert attributes["num_levels"] == levels_for(ref)
    assert 1 <= attributes["num_levels"] <= 6


def test_seeded_attributes_drive_a_real_compute(conn, migrated_db):
    """The seeder's output is what effective_numeric resolves.

    Guards the join between the two halves: a key written under a name the
    indicator does not read would leave every block skipped and the run
    reporting success with zero rows.
    """
    seed_catalog(conn)
    ref = f"seed-blk-{uuid4().hex[:8]}"
    block_id = _block(conn, {}, ref)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False)

    result = compute_block_housing_units(
        conn, feature_ids=[block_id], code_version="test"
    )
    assert result.features_considered == 1, "seeded block was skipped as incomplete"
    assert result.rows_written == 1

    written = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .get_current(
            feature_id=block_id, indicator_slug=EDIFICABILITY_BLOCK_HOUSING_UNITS.slug
        )
    )
    # ~220 m² at the seeded levels and 60 m²/unit.
    expected_levels = levels_for(ref)
    assert written["value_numeric"] > 0
    assert written["value_numeric"] < 220 * expected_levels / 60 * 1.05


def test_dry_run_writes_nothing(conn, migrated_db):
    block_id = _block(conn, {})
    conn.commit()

    result = seed(migrated_db, aoi_id=None, dry_run=True)
    assert result["written"] == 0
    assert result["dry_run"] is True
    assert result["blocks"] >= 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT attributes FROM curated_geo.feature WHERE id = %s::uuid",
            (block_id,),
        )
        assert "num_levels" not in cur.fetchone()[0]


# ─── Fitted to buildings and the census ──────────────────────────────────


def _census(conn, block_id: str, **values: float) -> None:
    """``vivtot=…, pobtot=…`` as the census loader writes them."""
    from reimagina.data import observation as obs

    source_id = _register_source(
        conn,
        name=f"censo_fit_{uuid4().hex[:8]}",
        storage_type="tabular",
        integration_role="enrichment",
    )
    run = _start_run(conn, source_id)
    for column, value in values.items():
        obs.upsert_observation(
            conn,
            feature_id=block_id,
            attribute_key=f"censo_2020_{column}",
            value_type="numeric",
            value=value,
            source_id=source_id,
            ingestion_run_id=run,
        )


def _real_floors(conn, block_id: str, floors: float) -> None:
    from reimagina.data import observation as obs

    source_id = _register_source(
        conn,
        name=f"storeys_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    obs.upsert_observation(
        conn,
        feature_id=block_id,
        attribute_key="floors",
        value_type="numeric",
        value=floors,
        source_id=source_id,
        ingestion_run_id=_start_run(conn, source_id),
    )


def _box(west: float, east: float, south=19.430000, north=19.430134) -> str:
    return (
        f"POLYGON(({west} {south},{east} {south},{east} {north},"
        f"{west} {north},{west} {south}))"
    )


def _building(conn, wkt: str) -> None:
    source_id = _register_source(
        conn,
        name=f"bldg_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "global", "building"),
        source_id=source_id,
        ingestion_run_id=_start_run(conn, source_id),
        dataset_version="v1",
        source_record_ref=f"bldg-{uuid4().hex[:8]}",
        geometry_wkt=wkt,
    )


def _west_half_building(conn) -> None:
    """A building over the west half of ``_WKT``, so coverage is 0.5."""
    source_id = _register_source(
        conn,
        name=f"bldg_{uuid4().hex[:8]}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    _insert_feature(
        conn,
        type_id=feat.get_type_id(conn, "global", "building"),
        source_id=source_id,
        ingestion_run_id=_start_run(conn, source_id),
        dataset_version="v1",
        source_record_ref=f"bldg-{uuid4().hex[:8]}",
        geometry_wkt=(
            "POLYGON((-99.140000 19.430000,-99.1399295 19.430000,"
            "-99.1399295 19.430134,-99.140000 19.430134,-99.140000 19.430000))"
        ),
    )


def _computed(conn, block_id: str, slug_compute) -> float | None:
    seed_catalog(conn)
    compute, slug = slug_compute
    compute(conn, feature_ids=[block_id], code_version="test")
    row = (
        backend_for_connection(conn)
        .indicator_result_repo()
        .get_current(feature_id=block_id, indicator_slug=slug)
    )
    return row["value_numeric"] if row else None


_HOUSING = (compute_block_housing_units, EDIFICABILITY_BLOCK_HOUSING_UNITS.slug)
_POPULATION = (compute_block_population, EDIFICABILITY_BLOCK_POPULATION.slug)


def _attributes(conn, block_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attributes FROM curated_geo.feature WHERE id = %s::uuid",
            (block_id,),
        )
        return cur.fetchone()[0]


def test_fitted_baseline_gives_the_census_dwellings_and_population(conn, migrated_db):
    """Coverage from the footprint, m² per dwelling from built floor area, and
    people per dwelling from the census: the formula gives the census back."""
    block_id = _block(conn, {})
    _west_half_building(conn)
    # ~110 m² of building: 3 dwellings is ~37 m² each, above the 20 m² floor.
    _census(conn, block_id, vivtot=3, pobtot=8)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    attributes = _attributes(conn, block_id)
    assert abs(attributes["lot_coverage"] - 0.5) < 0.01
    assert attributes["num_levels"] == attributes["floors"] == 1.0
    assert "estimated" not in attributes
    assert abs(_computed(conn, block_id, _HOUSING) - 3) < 1e-6
    assert abs(_computed(conn, block_id, _POPULATION) - 8) < 1e-6


def test_a_thin_footprint_is_estimated_and_still_gives_the_census(conn, migrated_db):
    """30 dwellings on a ~220 m² block is under 8 m² each whatever is mapped
    on it (other tests' buildings persist here): a mapping gap. The block is
    fitted like an unmapped one and marked; a re-seed with a census it can
    hold clears the mark."""
    block_id = _block(conn, {})
    _west_half_building(conn)
    _census(conn, block_id, vivtot=30, pobtot=80)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    assert _attributes(conn, block_id)["estimated"] == "thin_footprint"
    assert abs(_computed(conn, block_id, _HOUSING) - 30) < 1e-6
    assert abs(_computed(conn, block_id, _POPULATION) - 80) < 1e-6

    _census(conn, block_id, vivtot=3, pobtot=8)
    conn.commit()
    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)
    assert "estimated" not in _attributes(conn, block_id)


def test_a_block_the_census_found_empty_is_not_built_on(conn, migrated_db):
    block_id = _block(conn, {})
    _census(conn, block_id, vivtot=0, pobtot=0)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    attributes = _attributes(conn, block_id)
    assert "unit_size" not in attributes
    assert _computed(conn, block_id, _HOUSING) is None
    # Planned floors still has a building height to show.
    assert attributes["floors"] >= 1


def test_a_real_height_never_builds_homes_the_census_did_not_count(conn, migrated_db):
    """Real storeys feed planned floors; the dwelling count stays the census."""
    block_id = _block(conn, {"unit_size": 60})
    _census(conn, block_id, vivtot=0)
    _real_floors(conn, block_id, 3.0)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    # The earlier seed's unit_size is gone, not kept by the merge.
    assert "unit_size" not in _attributes(conn, block_id)
    assert _computed(conn, block_id, _HOUSING) is None


def test_fitted_dwellings_use_the_real_height_where_there_is_one(conn, migrated_db):
    block_id = _block(conn, {})
    _census(conn, block_id, vivtot=4, pobtot=10)
    _real_floors(conn, block_id, 3.0)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    assert _attributes(conn, block_id)["num_levels"] == 3
    assert abs(_computed(conn, block_id, _HOUSING) - 4) < 1e-6


def test_overlapping_buildings_cover_their_union_not_their_sum(conn, migrated_db):
    """Two footprints that overlap on a quarter of the block cover three
    quarters of it. Summed, they claimed the whole block — Overture carries
    overlapping and duplicated footprints — and inflated m² per dwelling; the
    census is given back either way, since m² per dwelling is fitted from the
    same coverage."""
    west, east = -99.150000, -99.149859
    width = east - west
    block_id = _block(conn, {}, wkt=_box(west, east))
    _building(conn, _box(west, west + width / 2))
    _building(conn, _box(west + width / 4, west + 3 * width / 4))
    _census(conn, block_id, vivtot=3, pobtot=8)
    conn.commit()

    seed(migrated_db, aoi_id=None, dry_run=False, fit_to_census=True)

    attributes = _attributes(conn, block_id)
    assert abs(attributes["lot_coverage"] - 0.75) < 0.01
    assert "estimated" not in attributes
    assert abs(_computed(conn, block_id, _HOUSING) - 3) < 1e-6
    assert abs(_computed(conn, block_id, _POPULATION) - 8) < 1e-6
