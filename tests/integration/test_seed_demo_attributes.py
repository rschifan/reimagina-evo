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
    seed_catalog,
)
from reimagina.indicators.edificability import compute_block_housing_units
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


def _block(conn, attributes):
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
        source_record_ref=f"seed-blk-{uuid4().hex[:8]}",
        geometry_wkt=_WKT,
        attributes=attributes,
    )


def test_seed_merges_and_preserves_existing_attributes(conn, migrated_db):
    """`||`, not assignment: CVEGEO must survive the seed."""
    block_id = _block(conn, {"CVEGEO": "0901500011234", "AMBITO": "Urbana"})
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
    assert attributes["num_levels"] == levels_for(block_id)
    assert 1 <= attributes["num_levels"] <= 6


def test_seeded_attributes_drive_a_real_compute(conn, migrated_db):
    """The seeder's output is what effective_numeric resolves.

    Guards the join between the two halves: a key written under a name the
    indicator does not read would leave every block skipped and the run
    reporting success with zero rows.
    """
    seed_catalog(conn)
    block_id = _block(conn, {})
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
    expected_levels = levels_for(block_id)
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
