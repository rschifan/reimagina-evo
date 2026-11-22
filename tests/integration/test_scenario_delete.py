"""Deleting a scenario — and the traces that outlive the row if you let it.

``app_core.scenario`` cascades its rules and, since r055, every row that
carries its id: overrides, results, job runs and AOI summaries. Published
versions and recorded materialisations are keyed by the *string*
``scenario=<id>``, which no constraint can follow, so those are deleted by
hand — and they are not quiet. ``asset_component_v`` (r041) unions
materialisation rows into its node set with no filter at all, so a deleted
scenario's partition would stay a live node in the graph — and, with its
override versions retired out from under it, a permanently *stale* one.

The other trace is the one a delete cannot see: a compute unit still running
for the scenario. It must either finish before the delete (which then removes
what it wrote) or find the scenario gone and write nothing.
"""

from __future__ import annotations

import threading
import warnings

import psycopg2
import pytest

from reimagina.assets.keys import partition_key, scenario_override_asset_key
from reimagina.assets.registry import seed_assets
from reimagina.data import aoi as aoi_mod, feature as feat
from reimagina.indicators.catalog import EDIFICABILITY_BLOCK_DENSITY, seed_catalog
from reimagina.orchestration.runners import default_runners
from reimagina.scenarios import authoring
from reimagina.scenarios.authoring import (
    DuplicateScenarioName,
    ScenarioNotFound,
    add_rule,
    create_scenario,
    delete_scenario,
    get_scenario,
    materialize_scenario,
)
from tests.integration._helpers import (
    discard,
    insert_feature as _insert_feature,
    insert_project,
    register_source as _register_source,
    start_run as _start_run,
)

_DENSITY = EDIFICABILITY_BLOCK_DENSITY.slug

#: The three numbers ``edificability.py`` needs together — it skips a block
#: missing any one of them, so a one-number scenario computes no results and
#: the supersession step would be tested against an empty set. Storeys write
#: two attributes, so the rule writes four.
_POLICY = {"levels": 6.0, "unit_size": 60.0, "people_per_household": 3.6}

# Four ~210 m squares at 19.40°N, and one rectangle that covers all of them.
_LAT0, _LAT1 = 19.4000, 19.4020
_BLOCK_LON0 = (-99.2000, -99.1960, -99.1920, -99.1880)
_BLOCK_SIDE = 0.0020
_AREA = (
    "POLYGON((-99.2010 19.3990,-99.1850 19.3990,"
    "-99.1850 19.4030,-99.2010 19.4030,-99.2010 19.3990))"
)


def _block_wkt(i: int) -> str:
    lon = _BLOCK_LON0[i]
    return (
        f"POLYGON(({lon} {_LAT0},{lon + _BLOCK_SIDE} {_LAT0},"
        f"{lon + _BLOCK_SIDE} {_LAT1},{lon} {_LAT1},{lon} {_LAT0}))"
    )


class _Territory:
    """A project, four blocks, an AOI covering them, and the asset graph seeded.

    Smaller than the materialisation fixture on purpose: deletion does not care
    where a shape fell, only what the scenario left behind, so one area over
    every block is the whole geometry this file needs.
    """

    def __init__(self, conn, *, suffix: str = ""):
        self.conn = conn
        self.source_name = f"delete_src{suffix}"
        feat.seed_feature_types(conn)
        seed_assets(conn)
        seed_catalog(conn)
        self.project_id = insert_project(conn, f"Test Project{suffix}")
        self.type_id = feat.get_type_id(conn, "MEX", "block")
        source_id = _register_source(
            conn,
            name=self.source_name,
            storage_type="vector",
            integration_role="canonical_entity",
        )
        ingest = _start_run(conn, source_id)
        self.blocks = [
            _insert_feature(
                conn,
                type_id=self.type_id,
                source_id=source_id,
                ingestion_run_id=ingest,
                dataset_version="v1",
                source_record_ref=f"del-blk-{i}",
                geometry_wkt=_block_wkt(i),
                attributes={},
            )
            for i in range(len(_BLOCK_LON0))
        ]
        self.aoi_id = aoi_mod.create_aoi(
            self.conn, project_id=self.project_id, name="all", geometry_wkt=_AREA
        )
        aoi_mod.compute_aoi_features(self.conn, self.aoi_id)

    def scenario(self, name: str = "Centralidad norte") -> str:
        """A scenario with one rule setting the three policy numbers everywhere."""
        scenario_id = create_scenario(
            self.conn, project_id=self.project_id, name=name, aoi_id=self.aoi_id
        )
        add_rule(
            self.conn,
            scenario_id=scenario_id,
            geometry_wkt=_AREA,
            type_id=self.type_id,
            inputs=dict(_POLICY),
        )
        return scenario_id


def _compute_indicator(conn, slug: str, *, scenario_id: str) -> None:
    """Compute one indicator the way the ops plane does.

    Through ``default_runners`` rather than the compute fn directly, because
    the runner is the seam that records the ``asset_materialization`` row — and
    that row is the trace the fourth deletion step exists to remove. Calling
    the compute fn would write results and leave the graph untouched, which is
    precisely the half of the state this file is about.
    """
    default_runners()[slug](
        conn, features=[], scenario_id=scenario_id, code_version="test", aoi_id=None
    )


def _override_count(conn, scenario_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.scenario_override WHERE scenario_id = %s",
            (scenario_id,),
        )
        return int(cur.fetchone()[0])


def _current_version(conn, scenario_id, attribute_key):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT av.version
            FROM curated_geo.asset_version av
            JOIN curated_geo.asset a ON a.id = av.asset_id
            WHERE a.asset_key = %s AND av.partition_key = %s AND av.is_current
            """,
            (
                scenario_override_asset_key(attribute_key),
                partition_key({"scenario": scenario_id}),
            ),
        )
        row = cur.fetchone()
        return None if row is None else row[0]


def _current_results(conn, scenario_id, slug) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT feature_id::text FROM curated_geo.indicator_result "
            "WHERE scenario_id IS NOT DISTINCT FROM %s AND indicator_slug = %s "
            "AND status = 'current' "
            "ORDER BY feature_id",
            (scenario_id, slug),
        )
        return [r[0] for r in cur.fetchall()]


def _partition_version_count(conn, scenario_id) -> int:
    """Every version of the partition, current or retired."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.asset_version WHERE partition_key = %s",
            (partition_key({"scenario": scenario_id}),),
        )
        return int(cur.fetchone()[0])


def _job_run_count(conn, scenario_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM jobs_runtime.job_run "
            "WHERE scenario_id IS NOT DISTINCT FROM %s",
            (scenario_id,),
        )
        return int(cur.fetchone()[0])


def _result_statuses(conn, scenario_id) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT status FROM curated_geo.indicator_result "
            "WHERE scenario_id = %s",
            (scenario_id,),
        )
        return {r[0] for r in cur.fetchall()}


def _materialization_rows(conn, scenario_id) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.asset_key FROM curated_geo.asset_materialization m "
            "JOIN curated_geo.asset a ON a.id = m.asset_id "
            "WHERE m.partition_key = %s ORDER BY a.asset_key",
            (partition_key({"scenario": scenario_id}),),
        )
        return [r[0] for r in cur.fetchall()]


def _freshness_states(conn, scenario_id) -> set[str]:
    """Every state the asset graph reports for this scenario's partition."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM explore.asset_freshness_v WHERE partition_key = %s",
            (partition_key({"scenario": scenario_id}),),
        )
        return {r[0] for r in cur.fetchall()}


def _stale_assets(conn) -> list[dict]:
    """What ``python -m apps.cli.app assets stale`` would print.

    The same view and the same states as ``apps/cli/assets.py``; read on the
    rolled-back fixture connection rather than through the CLI, which opens its
    own committed one and could not see this test's rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT asset_key, kind, partition_key, state "
            "FROM explore.asset_freshness_v WHERE state = ANY(%s) "
            "ORDER BY state, asset_key, partition_key",
            (["stale", "partial"],),
        )
        return [
            {"asset_key": r[0], "kind": r[1], "partition_key": r[2], "state": r[3]}
            for r in cur.fetchall()
        ]


def _rule_count(conn, scenario_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.scenario_rule WHERE scenario_id = %s",
            (scenario_id,),
        )
        return int(cur.fetchone()[0])


# ── The five steps ──────────────────────────────────────────────────────────


def test_delete_removes_overrides_versions_results_and_materialisations(conn):
    t = _Territory(conn)
    sid = t.scenario()
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)
    assert _current_results(conn, sid, _DENSITY) != []
    assert _materialization_rows(conn, sid) != []

    result = delete_scenario(conn, scenario_id=sid)

    assert result.overrides == 16  # 4 blocks x 4 attributes
    assert result.versions_deleted == 4
    assert result.results_deleted == 4
    assert result.job_runs_deleted == 1
    assert result.materializations == 1
    assert _override_count(conn, sid) == 0
    assert _current_version(conn, sid, "num_levels") is None
    assert _current_results(conn, sid, _DENSITY) == []
    assert _materialization_rows(conn, sid) == []
    assert get_scenario(conn, sid) is None
    assert _rule_count(conn, sid) == 0  # cascaded


def test_delete_leaves_no_row_that_names_the_scenario(conn):
    """Nothing keyed by the scenario survives it, in any status.

    These rows used to be kept — results staled, versions retired — "as
    history". Nothing could read that history: every surface that shows a
    scenario's numbers starts from the scenario row, so once it is gone the
    rows were unreachable and only grew (614,880 of them for three deleted
    scenarios on the pilot database). The job_run goes too: its scenario_id is
    the one the r016 trigger forces every result of it to carry, so it ran for
    this scenario and nothing else.
    """
    t = _Territory(conn)
    sid = t.scenario()
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)
    # Run twice, so the first run's rows are already stale history when the
    # delete arrives: the old step only touched `current` ones.
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)
    assert "stale" in _result_statuses(conn, sid)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO curated_geo.aoi_indicator_summary "
            "(aoi_id, scenario_id, indicator_id, value) "
            "SELECT %s, %s, id, 1 FROM curated_geo.indicator_catalog WHERE slug = %s",
            (t.aoi_id, sid, _DENSITY),
        )

    delete_scenario(conn, scenario_id=sid)

    assert _result_statuses(conn, sid) == set()
    assert _partition_version_count(conn, sid) == 0
    assert _job_run_count(conn, sid) == 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.aoi_indicator_summary "
            "WHERE scenario_id = %s",
            (sid,),
        )
        assert cur.fetchone()[0] == 0


def test_delete_leaves_the_baseline_alone(conn):
    """Scoped by scenario id: the baseline's results and runs share both tables."""
    t = _Territory(conn)
    sid = t.scenario()
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=None)
    baseline = _current_results(conn, None, _DENSITY)

    delete_scenario(conn, scenario_id=sid)

    assert _current_results(conn, None, _DENSITY) == baseline
    assert _job_run_count(conn, None) >= 1


def test_the_name_is_reusable_after_delete(conn):
    """``UNIQUE (project_id, name)`` must not outlive the row that owned it."""
    t = _Territory(conn)
    sid = t.scenario(name="Centralidad norte")
    materialize_scenario(conn, scenario_id=sid)

    delete_scenario(conn, scenario_id=sid)

    create_scenario(
        conn,
        project_id=t.project_id,
        name="Centralidad norte",
        aoi_id=t.aoi_id,
    )


def test_a_second_scenario_in_the_project_is_untouched(conn):
    """Deletion is scoped by scenario id, not by project.

    Two what-ifs over the same blocks share every table this deletion writes
    to, and three of the five steps are filtered by ``scenario_id`` alone —
    widening any of them to the project would silently wipe the neighbouring
    proposal.
    """
    t = _Territory(conn)
    doomed = t.scenario(name="Doomed")
    keeper = t.scenario(name="Keeper")
    materialize_scenario(conn, scenario_id=doomed)
    materialize_scenario(conn, scenario_id=keeper)
    _compute_indicator(conn, _DENSITY, scenario_id=keeper)

    delete_scenario(conn, scenario_id=doomed)

    assert _override_count(conn, keeper) == 16
    assert _current_version(conn, keeper, "num_levels") is not None
    assert _current_results(conn, keeper, _DENSITY) != []
    assert _materialization_rows(conn, keeper) != []
    assert get_scenario(conn, keeper) is not None


# ── The fourth step, which is not cosmetic ──────────────────────────────────


def test_a_deleted_scenario_leaves_no_stale_asset_entry(conn):
    """``assets stale`` must not accumulate one line per deleted scenario.

    The materialisation row is the only thing keeping the partition in
    ``asset_component_v``'s node set once the override versions are retired —
    and a node whose upstream components have all fallen to ``'missing'``
    reports ``stale`` for ever, because nothing will ever recompute a scenario
    that no longer exists.
    """
    t = _Territory(conn)
    sid = t.scenario()
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)
    # The partition is a real node in the graph before the delete, so its
    # absence afterwards is a removal rather than a fixture that never existed.
    assert _freshness_states(conn, sid) != set()

    delete_scenario(conn, scenario_id=sid)

    partition = partition_key({"scenario": sid})
    assert partition not in {r["partition_key"] for r in _stale_assets(conn)}
    # Nor in any other state: the partition is not a node in the graph at all.
    assert _freshness_states(conn, sid) == set()


# ── Refusals ────────────────────────────────────────────────────────────────


def test_deleting_an_unknown_scenario_raises(conn):
    """The lock is also the existence check, and it is taken first.

    A delete that silently reports four zeros for a scenario that was never
    there is a 200 on a typo. ``_lock_scenario`` already answers this, which is
    one more reason for it to be the first statement rather than a step buried
    between the deletes.
    """
    _Territory(conn)
    with pytest.raises(LookupError):
        delete_scenario(conn, scenario_id="00000000-0000-0000-0000-000000000000")


def test_deleting_a_scenario_that_was_never_materialised_is_not_an_error(conn):
    """Nothing to retire, nothing to stale — and still a scenario to remove.

    No rule: drawing one materialises it.
    """
    t = _Territory(conn)
    sid = create_scenario(conn, project_id=t.project_id, name="Bare", aoi_id=t.aoi_id)

    result = delete_scenario(conn, scenario_id=sid)

    assert (result.overrides, result.versions_deleted) == (0, 0)
    assert (result.results_deleted, result.materializations) == (0, 0)
    assert result.job_runs_deleted == 0
    assert get_scenario(conn, sid) is None
    assert _rule_count(conn, sid) == 0


def test_a_duplicate_name_is_still_refused_while_the_scenario_lives(conn):
    """The reuse test above must be proving a deletion, not a missing CHECK."""
    t = _Territory(conn)
    t.scenario(name="Centralidad norte")

    with pytest.raises(DuplicateScenarioName):
        create_scenario(
            conn,
            project_id=t.project_id,
            name="Centralidad norte",
            aoi_id=t.aoi_id,
        )


# ── Concurrency ─────────────────────────────────────────────────────────────


def test_a_concurrent_materialisation_cannot_outlive_the_delete(
    migrated_db, monkeypatch
):
    """A materialisation in flight must not re-populate a scenario being deleted.

    ``c1`` resolves the scenario's rules and then pauses, exactly as the
    materialisation concurrency test does, before any override row is written.
    ``c2`` deletes the scenario. Without the row lock as ``delete_scenario``'s
    first act, ``c2`` sees nothing to remove, drops the scenario row and
    commits; ``c1`` then resumes and writes overrides and publishes versions
    for a scenario that no longer exists — orphan rows no FK will ever collect
    and a live partition in the asset graph pointing at nothing.

    With the lock, ``c2`` waits at its first statement until ``c1`` commits and
    then deletes what ``c1`` wrote, so the end state is the one the delete
    asked for either way the race is run.

    The shared ``conn`` fixture only rolls back, so a second connection could
    not see its rows. This test commits its own setup on its own connections
    and removes it afterwards — including the ``curated_geo.asset_version``
    rows, which hang off ``curated_geo.asset`` and are reachable from neither
    the project nor the scenario by cascade.
    """
    c1 = psycopg2.connect(migrated_db)
    c2 = psycopg2.connect(migrated_db)
    setup = psycopg2.connect(migrated_db)
    territory = None
    sid = None
    try:
        territory = _Territory(setup, suffix="_delete_concurrency")
        sid = territory.scenario(name="Raced")
        setup.commit()

        resolved = threading.Event()
        delete_done = threading.Event()
        errors: list[BaseException] = []
        unpaused = authoring._refuse_ceiling_breaches

        def pause_after_resolution(conn, *args, **kwargs):
            unpaused(conn, *args, **kwargs)
            if conn is c1:  # only the run this test holds open
                resolved.set()
                # Bounded: with the lock, c2 is blocked and never arrives.
                delete_done.wait(timeout=2.0)

        monkeypatch.setattr(
            authoring, "_refuse_ceiling_breaches", pause_after_resolution
        )

        def stale_run() -> None:
            try:
                materialize_scenario(c1, scenario_id=sid)
                c1.commit()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        held = threading.Thread(target=stale_run)
        held.start()
        try:
            assert resolved.wait(timeout=30), "the held run never reached the pause"
            try:
                delete_scenario(c2, scenario_id=sid)
                c2.commit()
            finally:
                delete_done.set()
        finally:
            held.join(timeout=30)

        assert not held.is_alive()
        assert not errors, errors
        assert get_scenario(setup, sid) is None
        assert _override_count(setup, sid) == 0
        assert _current_version(setup, sid, "num_levels") is None
    finally:
        for c in (c1, c2):
            try:
                c.rollback()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"rollback failed: {exc}", stacklevel=2)
        setup.rollback()
        if territory is not None:
            if sid is not None:
                discard(
                    setup,
                    "DELETE FROM curated_geo.scenario_override WHERE scenario_id = %s",
                    sid,
                )
                # Not reachable by cascade from anything this test deletes:
                # asset_version hangs off curated_geo.asset, not the scenario.
                discard(
                    setup,
                    "DELETE FROM curated_geo.asset_version WHERE partition_key = %s",
                    partition_key({"scenario": sid}),
                )
                discard(
                    setup,
                    "DELETE FROM curated_geo.asset_materialization "
                    "WHERE partition_key = %s",
                    partition_key({"scenario": sid}),
                )
            # Cascades scenario, scenario_rule, area_of_interest, aoi_feature.
            discard(
                setup,
                "DELETE FROM app_core.project WHERE id = %s",
                territory.project_id,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
                territory.blocks,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                territory.source_name,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                territory.source_name,
            )
        for c in (c1, c2, setup):
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"close failed: {exc}", stacklevel=2)


def test_removing_a_rule_while_its_scenario_is_deleted_does_not_deadlock(
    migrated_db,
):
    """``delete_rule`` and ``delete_scenario`` take their locks in one order.

    ``delete_scenario`` locks the scenario row first and reaches the rules
    last, through the cascade. A remover that deleted the rule first and then
    touched the scenario's ``updated_at`` would hold what the delete waits for
    and wait for what it holds, and Postgres would abort one of them with
    ``DeadlockDetected``.

    Deterministic rather than timed: the delete takes its lock, the remover is
    started, and the test waits until Postgres reports that backend blocked on
    a lock before letting the delete run on to its cascade. With the lock
    taken first, the remover is blocked before it has touched the rule, so the
    delete finishes and the remover then finds no scenario.
    """
    c_del = psycopg2.connect(migrated_db)
    c_rm = psycopg2.connect(migrated_db)
    setup = psycopg2.connect(migrated_db)
    territory = None
    sid = None
    try:
        territory = _Territory(setup, suffix="_remove_vs_delete")
        sid = territory.scenario(name="Removed while deleted")
        setup.commit()
        rule_id = get_scenario(setup, sid)["rules"][0]["id"]

        with c_del.cursor() as cur:
            authoring._lock_scenario(cur, sid, mode="UPDATE")

        outcome: list[object] = []

        def remove() -> None:
            try:
                authoring.delete_rule(c_rm, scenario_id=sid, rule_id=rule_id)
                outcome.append("deleted")
                c_rm.commit()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                outcome.append(exc)

        rm_pid = c_rm.get_backend_pid()
        remover = threading.Thread(target=remove)
        remover.start()
        try:
            with setup.cursor() as cur:
                for _ in range(300):
                    cur.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        (rm_pid,),
                    )
                    row = cur.fetchone()
                    setup.rollback()
                    if row is not None and row[0] == "Lock":
                        break
                    threading.Event().wait(0.05)
                else:
                    pytest.fail("delete_rule never blocked on a lock")

            delete_scenario(c_del, scenario_id=sid)
            c_del.commit()
        finally:
            remover.join(timeout=30)

        assert not remover.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], ScenarioNotFound), outcome
        assert get_scenario(setup, sid) is None
    finally:
        for c in (c_del, c_rm):
            try:
                c.rollback()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"rollback failed: {exc}", stacklevel=2)
        setup.rollback()
        if territory is not None:
            if sid is not None:
                for table in ("asset_version", "asset_materialization"):
                    discard(
                        setup,
                        f"DELETE FROM curated_geo.{table} WHERE partition_key = %s",
                        partition_key({"scenario": sid}),
                    )
            discard(
                setup,
                "DELETE FROM app_core.project WHERE id = %s",
                territory.project_id,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
                territory.blocks,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                territory.source_name,
            )
            discard(
                setup,
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                territory.source_name,
            )
        for c in (c_del, c_rm, setup):
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"close failed: {exc}", stacklevel=2)


# ── In-flight compute units (B2, B3) ────────────────────────────────────────


def _committed_territory(migrated_db, suffix):
    setup = psycopg2.connect(migrated_db)
    seed_catalog(setup)
    seed_assets(setup)
    territory = _Territory(setup, suffix=suffix)
    sid = territory.scenario(name=f"In flight{suffix}")
    materialize_scenario(setup, scenario_id=sid)
    setup.commit()
    return setup, territory, sid


def _tidy(setup, territory, sid):
    setup.rollback()
    for table in ("asset_version", "asset_materialization"):
        discard(
            setup,
            f"DELETE FROM curated_geo.{table} WHERE partition_key = %s",
            partition_key({"scenario": sid}),
        )
    discard(setup, "DELETE FROM app_core.project WHERE id = %s", territory.project_id)
    discard(
        setup,
        "DELETE FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
        territory.blocks,
    )
    discard(
        setup,
        "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
        "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
        territory.source_name,
    )
    discard(
        setup,
        "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
        territory.source_name,
    )
    setup.close()


def _wait_until_blocked(setup, pid) -> None:
    with setup.cursor() as cur:
        for _ in range(300):
            cur.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            )
            row = cur.fetchone()
            setup.rollback()
            if row is not None and row[0] == "Lock":
                return
            threading.Event().wait(0.05)
    pytest.fail("the second connection never blocked on a lock")


def _orphans(conn, sid) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM curated_geo.indicator_result "
            "        WHERE scenario_id = %s), "
            "       (SELECT count(*) FROM jobs_runtime.job_run WHERE scenario_id = %s)",
            (sid, sid),
        )
        return cur.fetchone()


def test_a_delete_waits_for_a_unit_in_flight_and_removes_what_it_wrote(migrated_db):
    """B3: the unit's writes cannot land after the delete.

    A unit whose compute is done holds ``KEY SHARE`` on the scenario row
    until it commits (``runners._fenced``), so the delete's ``FOR UPDATE``
    waits for it and then deletes its results with the rest.
    Without it the delete counted what existed, committed, and the unit then
    committed 19,058 results and a job run under a deleted id.
    """
    setup, territory, sid = _committed_territory(migrated_db, "_inflight")
    worker = psycopg2.connect(migrated_db)
    deleter = psycopg2.connect(migrated_db)
    try:
        runner = default_runners()[_DENSITY]
        runner(
            worker, features=frozenset(), scenario_id=sid, code_version="t", aoi_id=None
        )
        done: list[object] = []

        def delete() -> None:
            try:
                done.append(delete_scenario(deleter, scenario_id=sid))
                deleter.commit()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                done.append(exc)

        thread = threading.Thread(target=delete)
        thread.start()
        _wait_until_blocked(setup, deleter.get_backend_pid())
        worker.commit()
        thread.join(timeout=30)

        assert done and not isinstance(done[0], BaseException), done
        assert done[0].results_deleted == 4
        assert _orphans(setup, sid) == (0, 0)
    finally:
        for c in (worker, deleter):
            c.rollback()
            c.close()
        _tidy(setup, territory, sid)


def test_a_unit_of_a_deleted_scenario_writes_nothing(migrated_db):
    setup, territory, sid = _committed_territory(migrated_db, "_after")
    worker = psycopg2.connect(migrated_db)
    try:
        delete_scenario(setup, scenario_id=sid)
        setup.commit()

        runner = default_runners()[_DENSITY]
        result = runner(
            worker, features=frozenset(), scenario_id=sid, code_version="t", aoi_id=None
        )
        worker.commit()

        assert result is None
        assert _orphans(setup, sid) == (0, 0)
        assert _materialization_rows(setup, sid) == []
    finally:
        worker.rollback()
        worker.close()
        _tidy(setup, territory, sid)


def test_two_units_of_one_scenario_and_indicator_run_one_after_the_other(
    migrated_db,
):
    """B2: two runs of one scenario computing the same indicator at once.

    The second insert used to hit ``ir_one_current_scenario`` once the first
    committed and fail the unit. It now waits on the (scenario, indicator)
    lock, then supersedes the first's rows.
    """
    setup, territory, sid = _committed_territory(migrated_db, "_twice")
    first = psycopg2.connect(migrated_db)
    second = psycopg2.connect(migrated_db)
    try:
        runner = default_runners()[_DENSITY]
        runner(
            first, features=frozenset(), scenario_id=sid, code_version="t", aoi_id=None
        )
        errors: list[BaseException] = []

        def run_second() -> None:
            try:
                runner(
                    second,
                    features=frozenset(),
                    scenario_id=sid,
                    code_version="t",
                    aoi_id=None,
                )
                second.commit()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        thread = threading.Thread(target=run_second)
        thread.start()
        _wait_until_blocked(setup, second.get_backend_pid())
        first.commit()
        thread.join(timeout=30)

        assert not errors, errors
        assert len(_current_results(setup, sid, _DENSITY)) == 4
    finally:
        for c in (first, second):
            c.rollback()
            c.close()
        delete_scenario(setup, scenario_id=sid)
        setup.commit()
        _tidy(setup, territory, sid)


def test_a_delete_does_not_wait_for_a_unit_still_computing(migrated_db):
    """A city-wide unit must not hold the delete for its whole compute.

    The unit here has opened its job run and written its results, then keeps
    computing (a barrier stands in for minutes of PostGIS). The delete must
    return at once; the unit, finding the scenario gone before it commits,
    writes nothing and ends without error — not with a ForeignKeyViolation.
    """
    from reimagina.orchestration import runners

    setup, territory, sid = _committed_territory(migrated_db, "_computing")
    worker = psycopg2.connect(migrated_db)
    deleter = psycopg2.connect(migrated_db)
    computing = threading.Event()
    release = threading.Event()
    real = runners._DIRECT_COMPUTE[_DENSITY]

    def slow_compute(conn, **kwargs):
        result = real(conn, **kwargs)
        computing.set()
        release.wait(timeout=30)
        return result

    outcome: list[object] = []

    def unit() -> None:
        try:
            runner = runners._scoped(_DENSITY, slow_compute)
            outcome.append(
                runner(
                    worker,
                    features=frozenset(),
                    scenario_id=sid,
                    code_version="t",
                    aoi_id=None,
                )
            )
            worker.commit()
        except BaseException as exc:  # noqa: BLE001 - asserted on below
            outcome.append(exc)

    thread = threading.Thread(target=unit)
    try:
        thread.start()
        assert computing.wait(timeout=30), "the unit never reached its compute"
        deleted: list[object] = []

        def delete() -> None:
            try:
                deleted.append(delete_scenario(deleter, scenario_id=sid))
                deleter.commit()
            except BaseException as exc:  # noqa: BLE001 - asserted on below
                deleted.append(exc)

        deleting = threading.Thread(target=delete)
        deleting.start()
        deleting.join(timeout=1.5)
        returned = not deleting.is_alive()
        release.set()
        deleting.join(timeout=30)
        thread.join(timeout=30)

        assert returned, "the delete waited for the unit's compute"
        assert deleted and not isinstance(deleted[0], BaseException), deleted
        assert outcome == [None], outcome
        assert _orphans(setup, sid) == (0, 0)
        assert _materialization_rows(setup, sid) == []
        assert _partition_version_count(setup, sid) == 0
        assert _override_count(setup, sid) == 0
        assert _rule_count(setup, sid) == 0
    finally:
        release.set()
        thread.join(timeout=30)
        for c in (worker, deleter):
            c.rollback()
            c.close()
        _tidy(setup, territory, sid)
