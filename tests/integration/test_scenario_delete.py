"""Deleting a scenario — and the four traces that outlive the row if you let it.

``app_core.scenario`` cascades exactly one thing: its edits. Everything else a
scenario accumulates is reachable only by its id, from tables that never heard
of the entity. ``scenario_override.scenario_id`` is a bare uuid (r030 deferred
``app_core.scenario`` to r048 and never went back to add the FK) and so is
``indicator_result.scenario_id``; published versions and recorded
materialisations are keyed by the *string* ``scenario=<id>``, which no
constraint can follow.

So ``DELETE FROM app_core.scenario`` on its own leaves four ghosts, and they
are not equally quiet. The loudest is the materialisation: ``asset_component_v``
(r041) unions materialisation rows into its node set with no filter at all, so
a deleted scenario's partition stays a live node in the graph — and, with its
override versions retired out from under it, a permanently *stale* one. Every
deleted scenario would add a line to ``assets stale`` naming a scenario nobody
can look up.
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
    add_edit,
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

#: The three attributes ``edificability.py`` needs together — it skips a block
#: missing any one of them, so a one-attribute scenario computes no results and
#: the supersession step would be tested against an empty set.
_POLICY = (("num_levels", 6.0), ("unit_size", 60.0), ("people_per_household", 3.6))

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
        """A scenario with all three policy attributes drawn over every block."""
        scenario_id = create_scenario(
            self.conn, project_id=self.project_id, name=name, aoi_id=self.aoi_id
        )
        for attribute_key, value in _POLICY:
            add_edit(
                self.conn,
                scenario_id=scenario_id,
                geometry_wkt=_AREA,
                type_id=self.type_id,
                attribute_key=attribute_key,
                value_numeric=value,
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
            "WHERE scenario_id = %s AND indicator_slug = %s AND status = 'current' "
            "ORDER BY feature_id",
            (scenario_id, slug),
        )
        return [r[0] for r in cur.fetchall()]


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


def _edit_count(conn, scenario_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.scenario_edit WHERE scenario_id = %s",
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

    assert result.overrides == 12  # 4 blocks x 3 attributes
    assert result.versions_retired == 3
    assert result.results_superseded == 4
    assert result.materializations == 1
    assert _override_count(conn, sid) == 0
    assert _current_version(conn, sid, "num_levels") is None
    assert _current_results(conn, sid, _DENSITY) == []
    assert _materialization_rows(conn, sid) == []
    assert get_scenario(conn, sid) is None
    assert _edit_count(conn, sid) == 0  # cascaded


def test_superseded_results_are_kept_as_stale_rather_than_deleted(conn):
    """The numbers the scenario produced are history, not garbage.

    ``'stale'`` is what the CHECK allows and what every other supersession in
    the platform writes; deleting the rows instead would make a what-if that
    was run, reported on and then tidied away indistinguishable from one that
    never computed anything.
    """
    t = _Territory(conn)
    sid = t.scenario()
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, _DENSITY, scenario_id=sid)

    delete_scenario(conn, scenario_id=sid)

    assert _result_statuses(conn, sid) == {"stale"}


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

    assert _override_count(conn, keeper) == 12
    assert _current_version(conn, keeper, "num_levels") is not None
    assert _current_results(conn, keeper, _DENSITY) != []
    assert _materialization_rows(conn, keeper) != []
    assert get_scenario(conn, keeper) is not None


# ── The fourth step, which the spec argues for by name ──────────────────────


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
    """Nothing to retire, nothing to stale — and still a scenario to remove."""
    t = _Territory(conn)
    sid = t.scenario()

    result = delete_scenario(conn, scenario_id=sid)

    assert (result.overrides, result.versions_retired) == (0, 0)
    assert (result.results_superseded, result.materializations) == (0, 0)
    assert get_scenario(conn, sid) is None
    assert _edit_count(conn, sid) == 0


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

    ``c1`` resolves the scenario's edits and then pauses, exactly as the
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

        def pause_after_resolution(conn, **kwargs):
            unpaused(conn, **kwargs)
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
            # Cascades scenario, scenario_edit, area_of_interest, aoi_feature.
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
