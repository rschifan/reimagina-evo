"""The plan the scenario materialisation SELECT runs, pinned.

``materialize_scenario`` holds a ``FOR UPDATE`` on the scenario row from its
first statement until the caller commits, so its cost is also the time every
other writer to that scenario waits. The statement that dominates it is the
one that resolves a drawn area to features:

    ST_Covers(e.geometry, f.geometry_centroid)

That is only cheap because ``feature_centroid_gist`` (r027) exists. Lose the
index, or write the predicate against ``f.geometry`` instead of the stored
centroid, and the same statement still returns the right rows, so nothing
functional fails; it just becomes a scan of every feature in the platform
while the lock is held.

This is a **plan** contract, not a timing budget. ``docs/architecture/
PERFORMANCE.md`` records the measured wall-clock figures and the machine and
method behind them.

The SQL under test is captured from ``_resolve_edits`` as it executes, not
copied here, so the contract follows the shipped statement.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory

from reimagina.data import aoi as aoi_mod, feature as feat
from reimagina.scenarios import authoring
from reimagina.scenarios.authoring import add_edit, create_scenario
from tests.integration._helpers import insert_project, register_source, start_run

#: Blocks in the fixture. 100 x 100 is large enough that, once the centroid
#: index is dropped, the planner prefers a sequential scan for a selective
#: area (see the control test below). A table of a few dozen rows would pass
#: the plan assertions because the planner would not bother with an index at
#: all, which would make the contract vacuous.
GRID = 100
BLOCK_SIDE = 0.002  # degrees, ~210 m
LON0, LAT0 = -99.30, 19.30

#: A rectangle over the south-west 3 x 3 blocks of the grid: selective.
SELECTIVE_AREA = (
    f"POLYGON(({LON0 - 0.0005} {LAT0 - 0.0005},{LON0 + 3 * BLOCK_SIDE} {LAT0 - 0.0005},"
    f"{LON0 + 3 * BLOCK_SIDE} {LAT0 + 3 * BLOCK_SIDE},{LON0 - 0.0005} {LAT0 + 3 * BLOCK_SIDE},"
    f"{LON0 - 0.0005} {LAT0 - 0.0005}))"
)


def build_territory(conn, *, grid: int = GRID) -> dict:
    """``grid`` x ``grid`` blocks in one AOI, with one scenario ready to edit.

    Bulk-inserted with ``generate_series`` rather than the per-row helper, and
    ``ANALYZE``d, because the planner's choice depends on statistics a fresh
    table does not have.
    """
    feat.seed_feature_types(conn)
    type_id = feat.get_type_id(conn, "MEX", "block")
    source_id = register_source(
        conn,
        name="materialize_plan_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature
              (type_id, name, geometry, attributes,
               source_id, source_record_ref, ingestion_run_id, dataset_version)
            SELECT %(t)s, 'blk-' || i || '-' || j,
                   ST_MakeEnvelope(
                     %(lon)s + i * %(side)s, %(lat)s + j * %(side)s,
                     %(lon)s + (i + 1) * %(side)s, %(lat)s + (j + 1) * %(side)s,
                     4326),
                   '{"num_levels": 4, "unit_size": 60, "people_per_household": 3.5}'::jsonb,
                   %(src)s, 'blk-' || i || '-' || j, %(run)s, 'v1'
            FROM generate_series(0, %(n)s - 1) i, generate_series(0, %(n)s - 1) j
            """,
            {
                "t": type_id,
                "src": source_id,
                "run": run_id,
                "lon": LON0,
                "lat": LAT0,
                "side": BLOCK_SIDE,
                "n": grid,
            },
        )
    project_id = insert_project(conn, "materialize plan")
    far = LON0 + (grid + 1) * BLOCK_SIDE
    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=project_id,
        name="whole grid",
        geometry_wkt=(
            f"POLYGON(({LON0 - 0.01} {LAT0 - 0.01},{far} {LAT0 - 0.01},"
            f"{far} {far - LON0 + LAT0},{LON0 - 0.01} {far - LON0 + LAT0},"
            f"{LON0 - 0.01} {LAT0 - 0.01}))"
        ),
    )
    aoi_mod.compute_aoi_features(conn, aoi_id)
    scenario_id = create_scenario(
        conn, project_id=project_id, name="plan", aoi_id=aoi_id
    )
    with conn.cursor() as cur:
        cur.execute("ANALYZE curated_geo.feature")
        cur.execute("ANALYZE curated_geo.aoi_feature")
        cur.execute("ANALYZE curated_geo.scenario_edit")
    return {"type_id": type_id, "aoi_id": aoi_id, "scenario_id": scenario_id}


class _Recorder:
    """A cursor that remembers what it was asked to run."""

    def __init__(self, cursor, sink: list) -> None:
        self._cursor = cursor
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append((sql, params))
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)


class _RecordingConn:
    def __init__(self, conn, sink: list) -> None:
        self._conn = conn
        self._sink = sink

    def cursor(self, *args, **kwargs):
        return _Recorder(self._conn.cursor(*args, **kwargs), self._sink)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def materialize_select(conn, scenario_id: str, aoi_id: str) -> tuple[str, dict]:
    """The exact ``(sql, params)`` that step 2 of materialisation executes."""
    sink: list = []
    authoring._resolve_edits(_RecordingConn(conn, sink), scenario_id, aoi_id)
    assert len(sink) == 1, f"step 2 is expected to be one statement, saw {len(sink)}"
    return sink[0]


def explain(conn, sql: str, params: dict) -> str:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (COSTS OFF) " + sql, params)
        return "\n".join(row[0] for row in cur.fetchall())


def _scenario_with_selective_edit(conn) -> tuple[str, str]:
    t = build_territory(conn)
    add_edit(
        conn,
        scenario_id=t["scenario_id"],
        geometry_wkt=SELECTIVE_AREA,
        type_id=t["type_id"],
        attribute_key="num_levels",
        value_numeric=6,
    )
    with conn.cursor() as cur:
        cur.execute("ANALYZE curated_geo.scenario_edit")
    return t["scenario_id"], t["aoi_id"]


def test_the_test_database_is_at_the_migration_head(conn):
    """The fixture is only meaningful on the schema the code ships against.

    ``alembic upgrade head`` is a no-op on a database that is already at head,
    so a probe that left this one at some other revision would be invisible
    to every plan assertion below. Assert it rather than assume it.
    """
    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        assert [r[0] for r in cur.fetchall()] == [head]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'curated_geo' "
            "AND indexname = 'feature_centroid_gist'"
        )
        assert cur.fetchone() is not None


def test_the_materialisation_select_uses_the_centroid_index(conn):
    scenario_id, aoi_id = _scenario_with_selective_edit(conn)
    sql, params = materialize_select(conn, scenario_id, aoi_id)

    plan = explain(conn, sql, params)

    assert "Seq Scan on feature" not in plan, plan
    assert "feature_centroid_gist" in plan, plan
    # The index is used as the access path, not merely mentioned.
    assert "geometry_centroid @ e.geometry" in plan, plan


def test_the_seq_scan_assertion_is_reachable(conn):
    """Control 1: forbid index access and the scan appears.

    ``"Seq Scan on feature" not in plan`` is only a claim if the planner can
    produce that text for this statement on this fixture. Switching off
    index and bitmap scans for the transaction shows it does.
    """
    scenario_id, aoi_id = _scenario_with_selective_edit(conn)
    sql, params = materialize_select(conn, scenario_id, aoi_id)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("SET LOCAL enable_bitmapscan = off")

    plan = explain(conn, sql, params)

    assert "Seq Scan on feature" in plan, plan
    assert "feature_centroid_gist" not in plan, plan


def test_the_index_is_what_the_plan_depends_on(conn):
    """Control 2: drop the index, and the plan loses it.

    Dropped inside the test's transaction, rolled back with it. The planner
    does not fall back to ``Seq Scan on feature`` here: it walks
    ``feature_type_id_idx`` and applies ``ST_Covers`` to every feature of the
    type. That is the same cost, every block in the platform examined while
    the scenario lock is held, and it does not contain the text the first
    assertion forbids. So the index-name assertion is the load-bearing one;
    the ``Seq Scan`` assertion alone would pass on this plan.
    """
    scenario_id, aoi_id = _scenario_with_selective_edit(conn)
    sql, params = materialize_select(conn, scenario_id, aoi_id)
    with conn.cursor() as cur:
        cur.execute("DROP INDEX curated_geo.feature_centroid_gist")

    plan = explain(conn, sql, params)

    assert "feature_centroid_gist" not in plan, plan
    assert "Index Cond: (type_id = e.type_id)" in plan or "Seq Scan on feature" in plan


def test_the_select_is_written_against_the_stored_centroid(conn):
    """The index is on ``geometry_centroid``; the predicate must name it.

    ``ST_PointOnSurface(f.geometry)`` computed inline would be the same answer
    and a sequential scan, which the plan assertion catches only by its effect.
    This names the cause.
    """
    scenario_id, aoi_id = _scenario_with_selective_edit(conn)
    sql, _ = materialize_select(conn, scenario_id, aoi_id)

    assert "f.geometry_centroid" in sql
