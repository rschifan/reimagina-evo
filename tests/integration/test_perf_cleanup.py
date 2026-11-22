"""`perf cleanup` removes what `perf seed-observations` wrote, and nothing else.

The seed writes permanent rows into a real database: synthetic current
observations on real features, a perf source and its ingestion runs, a
'Performance Benchmark Project' and four ``perf_*`` AOIs. Each test plants a
non-perf look-alike next to them — an observation on the same feature, an AOI
with a perf name in another project — because a cleanup that matched by a
looser rule would pass every count and still delete someone's data.
"""

from __future__ import annotations

import pytest

from reimagina.data import aoi as aoi_mod, feature as feat
from reimagina.perf import cleanup_perf_data, prepare_benchmark_dataset

from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)


def _square(x0: float, y0: float, side: float = 0.01) -> str:
    x1, y1 = x0 + side, y0 + side
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


def _one(conn, sql: str, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _territory(conn) -> dict[str, str]:
    """Two real blocks, one real observation, one real AOI named like a perf one."""
    source_id = register_source(
        conn,
        name="real_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    block = feat.get_type_id(conn, "MEX", "block")
    features = [
        insert_feature(
            conn,
            type_id=block,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"real-{i}",
            geometry_wkt=_square(-99.14 + i * 0.01, 19.43),
        )
        for i in range(2)
    ]
    with conn.cursor() as cur:
        # The seed skips a (feature, attribute) pair that already has a current
        # row, so this one must survive both the seed and the cleanup.
        cur.execute(
            """
            INSERT INTO curated_geo.observation
              (feature_id, attribute_key, value_type, value_numeric,
               source_id, ingestion_run_id)
            VALUES (%s, 'population_total', 'numeric', 42, %s, %s)
            RETURNING id
            """,
            (features[0], source_id, run_id),
        )
        real_obs = str(cur.fetchone()[0])
    real_project = insert_project(conn, name="Real Project")
    real_aoi = aoi_mod.create_aoi(
        conn,
        project_id=real_project,
        name="perf_bbox_small",
        geometry_wkt=_square(-99.14, 19.43),
    )
    return {
        "source": source_id,
        "run": run_id,
        "obs": real_obs,
        "project": real_project,
        "aoi": real_aoi,
    }


def _perf_rows(conn) -> int:
    return _one(
        conn,
        """
        SELECT (SELECT count(*) FROM curated_geo.observation o
                  JOIN curated_geo.data_source_catalog s ON s.id = o.source_id
                 WHERE s.provider = 'reimagina-perf')
             + (SELECT count(*) FROM curated_geo.data_source_catalog
                 WHERE provider = 'reimagina-perf')
             + (SELECT count(*) FROM app_core.project
                 WHERE name = 'Performance Benchmark Project')
        """,
    )


def test_cleanup_removes_every_perf_row_and_only_those(conn):
    real = _territory(conn)
    prepare_benchmark_dataset(conn, dataset_tier="smoke")
    prepare_benchmark_dataset(conn, dataset_tier="smoke")  # a second source run
    before = _perf_rows(conn)
    # Counted, not assumed: the seed covers every feature of its types, and
    # other integration tests commit features into this same database.
    perf_obs = _one(
        conn,
        "SELECT count(*) FROM curated_geo.observation o "
        "JOIN curated_geo.data_source_catalog s ON s.id = o.source_id "
        "WHERE s.provider = 'reimagina-perf'",
    )
    assert perf_obs >= 9  # at least this test's 2 features x 5 keys - 1 real
    perf_members = _one(
        conn,
        "SELECT count(*) FROM curated_geo.aoi_feature af "
        "JOIN curated_geo.area_of_interest a ON a.id = af.aoi_id "
        "JOIN app_core.project p ON p.id = a.project_id "
        "WHERE p.name = 'Performance Benchmark Project'",
    )

    preview = cleanup_perf_data(conn, dry_run=True)
    assert preview["dry_run"] is True
    assert _perf_rows(conn) == before, "a dry run must delete nothing"

    removed = cleanup_perf_data(conn)
    assert removed["removed"] == preview["removed"]
    counts = removed["removed"]
    assert counts["curated_geo.observation"] == perf_obs
    assert counts["curated_geo.ingestion_run"] == 2
    assert counts["curated_geo.data_source_catalog"] == 1
    assert counts["curated_geo.area_of_interest"] == 4
    # Members depend on where the perf AOIs land in whatever the DB holds.
    assert counts["curated_geo.aoi_feature"] == perf_members
    assert counts["app_core.project"] == 1
    assert _perf_rows(conn) == 0

    for sql, key in (
        ("SELECT count(*) FROM curated_geo.observation WHERE id = %s", "obs"),
        ("SELECT count(*) FROM curated_geo.ingestion_run WHERE id = %s", "run"),
        (
            "SELECT count(*) FROM curated_geo.data_source_catalog WHERE id = %s",
            "source",
        ),
        ("SELECT count(*) FROM app_core.project WHERE id = %s", "project"),
        ("SELECT count(*) FROM curated_geo.area_of_interest WHERE id = %s", "aoi"),
    ):
        assert _one(conn, sql, real[key]) == 1, f"non-perf {key} was deleted"

    again = cleanup_perf_data(conn)
    assert set(again["removed"].values()) == {0}, "a second run must remove nothing"


def test_cleanup_refuses_while_real_rows_point_at_a_perf_aoi(conn):
    """Deleting the AOI would null `scenario.aoi_id` — a write to a real row."""
    _territory(conn)
    aoi_ids = prepare_benchmark_dataset(conn, dataset_tier="smoke")["aoi_ids"]
    project = _one(
        conn,
        "SELECT id FROM app_core.project WHERE name = 'Performance Benchmark Project'",
    )
    scenario = _one(
        conn,
        "INSERT INTO app_core.scenario (project_id, aoi_id, name) "
        "VALUES (%s, %s, 'someone''s plan') RETURNING id",
        project,
        aoi_ids["bbox_small"],
    )
    before = _perf_rows(conn)

    with pytest.raises(ValueError, match="app_core.scenario"):
        cleanup_perf_data(conn)
    assert _perf_rows(conn) == before

    # Unhooked, the AOIs go — but the project holds a real scenario, so it stays.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_core.scenario SET aoi_id = NULL WHERE id = %s", (scenario,)
        )
    counts = cleanup_perf_data(conn)["removed"]
    assert counts["curated_geo.area_of_interest"] == 4
    assert counts["app_core.project"] == 0
    assert _one(conn, "SELECT count(*) FROM app_core.scenario WHERE id = %s", scenario)
