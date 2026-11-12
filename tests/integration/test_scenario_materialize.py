"""Resolving a scenario's drawn rules into per-block overrides.

An edit is an area, an attribute and a value. Materialisation is what turns
that into the override rows the asset graph already partitions by scenario, so
the ordinary ``evaluate-scenario`` path recomputes exactly the affected
indicators.

The tests that matter least obviously are the removal ones. An override that
disappears leaves two ghosts: the retired *version* nobody reconciles, and the
``indicator_result`` row nobody stales — ``bulk_write_numeric_results`` marks
stale only the features it is writing, and a block that lost an input is no
longer written at all. The second ghost is the one a user sees, because
``serving_geo.indicator_overlay_tile`` keeps painting it.
"""

from __future__ import annotations

import threading
import warnings

import psycopg2
import pytest

from reimagina.data import aoi as aoi_mod, feature as feat
from reimagina.indicators.catalog import EDIFICABILITY_BLOCK_DENSITY, seed_catalog
from reimagina.indicators.edificability import compute_block_housing_density
from reimagina.assets.keys import partition_key, scenario_override_asset_key
from reimagina.assets.registry import seed_assets
from reimagina.scenarios import authoring
from reimagina.scenarios.authoring import (
    CeilingExceeded,
    ScenarioHasNoAOI,
    add_edit,
    create_scenario,
    materialize_scenario,
    remove_edit,
)
from tests.integration._helpers import (
    insert_feature as _insert_feature,
    insert_project,
    register_source as _register_source,
    start_run as _start_run,
)

_DENSITY = EDIFICABILITY_BLOCK_DENSITY.slug

# A row of six ~210 m squares at 19.40°N, each ~46,500 m². Spaced so a drawn
# rectangle can take any prefix of them without clipping the next.
_LAT0, _LAT1 = 19.4000, 19.4020
_BLOCK_LON0 = (-99.2000, -99.1960, -99.1920, -99.1880, -99.1840, -99.1800)
_BLOCK_SIDE = 0.0020

#: Every block (b0..b5). Also the narrow AOI's complement test subject.
_ALL = range(6)


def _rect(lon_min: float, lon_max: float, lat_min=_LAT0 - 0.001, lat_max=_LAT1 + 0.001):
    return (
        f"POLYGON(({lon_min} {lat_min},{lon_max} {lat_min},"
        f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min}))"
    )


def _block_wkt(i: int) -> str:
    lon = _BLOCK_LON0[i]
    return (
        f"POLYGON(({lon} {_LAT0},{lon + _BLOCK_SIDE} {_LAT0},"
        f"{lon + _BLOCK_SIDE} {_LAT1},{lon} {_LAT1},{lon} {_LAT0}))"
    )


#: A rectangle over blocks 0..3 — the four the "drawn area" tests select.
_AREA_0_3 = _rect(-99.2010, -99.1850)
#: A rectangle over all six.
_AREA_ALL = _rect(-99.2010, -99.1770)
#: A rectangle in the same neighbourhood that catches no block at all.
_AREA_EMPTY = _rect(-99.3000, -99.2900)
#: The narrow AOI: blocks 0..3 only.
_AOI_NARROW = _rect(-99.2010, -99.1850)
#: The wide AOI: all six.
_AOI_WIDE = _rect(-99.2010, -99.1770)


class _Territory:
    """What a materialisation test needs: a project, blocks, AOIs, a type."""

    def __init__(self, conn, *, attributes: dict | None = None, suffix: str = ""):
        self.conn = conn
        self.project_name = f"Test Project{suffix}"
        self.source_name = f"materialize_src{suffix}"
        self.project_id = insert_project(conn, self.project_name)
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
                source_record_ref=f"mat-blk-{i}",
                geometry_wkt=_block_wkt(i),
                attributes=dict(attributes or {}),
            )
            for i in _ALL
        ]
        self.aoi_wide = self._aoi("wide", _AOI_WIDE)
        self.aoi_narrow = self._aoi("narrow", _AOI_NARROW)

    def _aoi(self, name: str, wkt: str) -> str:
        aoi_id = aoi_mod.create_aoi(
            self.conn, project_id=self.project_id, name=name, geometry_wkt=wkt
        )
        aoi_mod.compute_aoi_features(self.conn, aoi_id)
        return aoi_id

    def add_block(self, ref: str, wkt: str, attributes: dict | None = None) -> str:
        """One more block, with the AOI memberships recomputed around it."""
        source_id = _register_source(
            self.conn,
            name=f"extra_src_{ref}",
            storage_type="vector",
            integration_role="canonical_entity",
        )
        feature_id = _insert_feature(
            self.conn,
            type_id=self.type_id,
            source_id=source_id,
            ingestion_run_id=_start_run(self.conn, source_id),
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=wkt,
            attributes=dict(attributes or {}),
        )
        for aoi_id in (self.aoi_wide, self.aoi_narrow):
            aoi_mod.compute_aoi_features(self.conn, aoi_id)
        return feature_id

    def scenario(self, name: str = "S", aoi_id: str | None = None) -> str:
        return create_scenario(
            self.conn,
            project_id=self.project_id,
            name=name,
            aoi_id=self.aoi_wide if aoi_id is None else aoi_id,
        )

    def edit(self, sid: str, wkt: str, key: str, value: float) -> str:
        return add_edit(
            self.conn,
            scenario_id=sid,
            geometry_wkt=wkt,
            type_id=self.type_id,
            attribute_key=key,
            value_numeric=value,
        )


def _override_value(conn, scenario_id, feature_id, attribute_key):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value_numeric FROM curated_geo.scenario_override "
            "WHERE scenario_id = %s AND feature_id = %s AND attribute_key = %s",
            (scenario_id, feature_id, attribute_key),
        )
        row = cur.fetchone()
        return None if row is None else row[0]


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


def _materialized_at(conn, scenario_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT materialized_at FROM app_core.scenario WHERE id = %s",
            (scenario_id,),
        )
        return cur.fetchone()[0]


def _set_aoi_null(conn, scenario_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_core.scenario SET aoi_id = NULL WHERE id = %s", (scenario_id,)
        )


def _compute_density(conn, scenario_id) -> None:
    compute_block_housing_density(conn, scenario_id=scenario_id, code_version="test")


# ── Selection ───────────────────────────────────────────────────────────────


def test_an_edit_overrides_the_blocks_its_area_covers(conn):
    t = _Territory(conn)
    sid = t.scenario()
    eid = t.edit(sid, _AREA_0_3, "num_levels", 6)

    result = materialize_scenario(conn, scenario_id=sid)

    assert result.matched_per_edit[eid] == 4
    assert result.overrides_written == 4
    assert _override_value(conn, sid, t.blocks[0], "num_levels") == 6
    assert _override_value(conn, sid, t.blocks[4], "num_levels") is None
    assert _materialized_at(conn, sid) is not None


def test_a_later_ordinal_wins_over_an_earlier_one(conn):
    t = _Territory(conn)
    sid = t.scenario()
    t.edit(sid, _AREA_0_3, "num_levels", 6)
    t.edit(sid, _AREA_ALL, "num_levels", 8)

    materialize_scenario(conn, scenario_id=sid)

    # b0 is in both areas; b4 only in the later one.
    assert _override_value(conn, sid, t.blocks[0], "num_levels") == 8
    assert _override_value(conn, sid, t.blocks[4], "num_levels") == 8
    assert _override_count(conn, sid) == 6


def test_a_half_covered_block_is_caught_by_its_centroid(conn):
    """The selection rule is ``ST_Covers(area, centroid)``, not containment.

    A block whose centroid is inside the drawn area but whose eastern half
    hangs out of it is caught — that is what a planner means by "did my shape
    get this block?", and it is what ``count_features_within_polygon`` (r027)
    and ``selection-filter.ts`` already answer. Testing the whole geometry with
    ``ST_CoveredBy`` instead would silently drop every edge block of a
    selection, which is exactly the boundary a district is drawn along.
    """
    t = _Territory(conn)
    # Centroid at -99.1860 (inside _AREA_0_3, which ends at -99.1850); the
    # block itself runs on to -99.1840, outside it.
    straddler = t.add_block(
        "straddler",
        "POLYGON((-99.1880 19.4005,-99.1840 19.4005,"
        "-99.1840 19.4015,-99.1880 19.4015,-99.1880 19.4005))",
    )
    sid = t.scenario()
    t.edit(sid, _AREA_0_3, "num_levels", 6)

    materialize_scenario(conn, scenario_id=sid)

    assert _override_value(conn, sid, straddler, "num_levels") == 6


def test_an_area_catching_no_blocks_reports_zero_and_writes_nothing(conn):
    t = _Territory(conn)
    sid = t.scenario()
    eid = t.edit(sid, _AREA_EMPTY, "num_levels", 6)

    result = materialize_scenario(conn, scenario_id=sid)

    assert result.matched_per_edit[eid] == 0
    assert result.overrides_written == 0
    assert _override_count(conn, sid) == 0


def test_blocks_outside_the_aoi_are_excluded_and_the_count_says_so(conn):
    t = _Territory(conn)
    sid = t.scenario(aoi_id=t.aoi_narrow)
    eid = t.edit(sid, _AREA_ALL, "num_levels", 6)

    result = materialize_scenario(conn, scenario_id=sid)

    assert result.matched_per_edit[eid] == 4
    assert _override_value(conn, sid, t.blocks[4], "num_levels") is None


def test_it_refuses_when_the_scenario_has_no_aoi(conn):
    t = _Territory(conn)
    sid = t.scenario()
    t.edit(sid, _AREA_0_3, "num_levels", 6)
    _set_aoi_null(conn, sid)

    with pytest.raises(ScenarioHasNoAOI):
        materialize_scenario(conn, scenario_id=sid)
    assert _override_count(conn, sid) == 0


# ── Idempotence and removal ─────────────────────────────────────────────────


def test_materialising_twice_changes_nothing_and_publishes_nothing(conn):
    feat.seed_feature_types(conn)
    seed_assets(conn)
    t = _Territory(conn)
    sid = t.scenario()
    t.edit(sid, _AREA_0_3, "num_levels", 6)

    first = materialize_scenario(conn, scenario_id=sid)
    version_before = _current_version(conn, sid, "num_levels")
    assert version_before is not None

    second = materialize_scenario(conn, scenario_id=sid)

    assert second.overrides_written == first.overrides_written
    assert second.overrides_removed == 0
    assert _current_version(conn, sid, "num_levels") == version_before


def test_removing_an_edit_removes_its_overrides_and_supersedes_its_results(conn):
    # ⚠ All three policy attributes, over the same four blocks.
    # ``edificability.py`` skips a feature missing any one of them, so a
    # one-edit fixture computes zero rows and this test would pass or fail for
    # a reason that has nothing to do with supersession.
    feat.seed_feature_types(conn)
    seed_assets(conn)
    seed_catalog(conn)
    t = _Territory(conn)
    sid = t.scenario()
    levels_eid = t.edit(sid, _AREA_0_3, "num_levels", 6)
    t.edit(sid, _AREA_0_3, "unit_size", 60)
    t.edit(sid, _AREA_0_3, "people_per_household", 3.6)

    materialize_scenario(conn, scenario_id=sid)
    _compute_density(conn, sid)
    assert _current_results(conn, sid, _DENSITY) != []

    remove_edit(conn, edit_id=levels_eid)
    result = materialize_scenario(conn, scenario_id=sid)

    assert result.overrides_removed == 4  # 4 blocks x 1 attribute
    assert result.results_superseded == 4
    assert _current_results(conn, sid, _DENSITY) == []


def test_removing_the_last_edit_for_an_attribute_retires_its_version(conn):
    feat.seed_feature_types(conn)
    seed_assets(conn)
    t = _Territory(conn)
    sid = t.scenario()
    eid = t.edit(sid, _AREA_0_3, "num_levels", 6)

    materialize_scenario(conn, scenario_id=sid)
    assert _current_version(conn, sid, "num_levels") is not None

    remove_edit(conn, edit_id=eid)
    materialize_scenario(conn, scenario_id=sid)

    assert _current_version(conn, sid, "num_levels") is None


# ── Ceilings ────────────────────────────────────────────────────────────────

#: Base attributes completing the edificability triple, so an edit that sets
#: only ``num_levels`` still resolves to a checkable block.
_BASE = {"unit_size": 60, "people_per_household": 3.6}


def test_a_ceiling_breach_refuses_the_whole_run(conn):
    t = _Territory(conn, attributes=_BASE)
    sid = t.scenario()
    t.edit(sid, _AREA_0_3, "num_levels", 13)  # 10000 * 13 / 60 = 2166 > 2000

    with pytest.raises(CeilingExceeded) as exc:
        materialize_scenario(conn, scenario_id=sid)

    assert exc.value.breaches[0].indicator_slug == _DENSITY
    assert {b.feature_id for b in exc.value.breaches} == set(t.blocks[:4])
    assert _override_count(conn, sid) == 0
    assert _materialized_at(conn, sid) is None


def test_one_district_breaching_does_not_refuse_on_a_blend(conn):
    """Two districts, two values, two block sizes — judged one block at a time.

    The small district is six levels on ~46,500 m² blocks; the tall one is
    thirteen levels on ~2.7 km² blocks. Checking the selection's *maximum*
    area against every block would put the small district over the
    housing-units bound as well (2.7e6 x 6 / 60 = 270,000 > 200,000), so the
    breach set is what distinguishes per-block checking from a blend.
    """
    conn_project = insert_project(conn)
    type_id = feat.get_type_id(conn, "MEX", "block")
    source_id = _register_source(
        conn,
        name="districts_src",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    ingest = _start_run(conn, source_id)

    def _make(ref, wkt):
        return _insert_feature(
            conn,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=ingest,
            dataset_version="v1",
            source_record_ref=ref,
            geometry_wkt=wkt,
            attributes=dict(_BASE),
        )

    small = [_make(f"small-{i}", _block_wkt(i)) for i in (0, 1)]
    # ~0.0155° square at 19.4°N: about 1.63 km x 1.72 km, ~2.8 km².
    tall = [
        _make(
            f"tall-{i}",
            f"POLYGON(({lon} 19.5000,{lon + 0.0155} 19.5000,"
            f"{lon + 0.0155} 19.5155,{lon} 19.5155,{lon} 19.5000))",
        )
        for i, lon in enumerate((-99.3000, -99.2800))
    ]
    aoi_id = aoi_mod.create_aoi(
        conn,
        project_id=conn_project,
        name="districts",
        geometry_wkt=_rect(-99.3100, -99.1770, 19.3900, 19.5300),
    )
    aoi_mod.compute_aoi_features(conn, aoi_id)

    sid = create_scenario(
        conn, project_id=conn_project, name="Two districts", aoi_id=aoi_id
    )
    short_area = _rect(-99.2010, -99.1930)
    tall_area = _rect(-99.3100, -99.2600, 19.4900, 19.5300)
    add_edit(
        conn,
        scenario_id=sid,
        geometry_wkt=short_area,
        type_id=type_id,
        attribute_key="num_levels",
        value_numeric=6,
    )
    add_edit(
        conn,
        scenario_id=sid,
        geometry_wkt=tall_area,
        type_id=type_id,
        attribute_key="num_levels",
        value_numeric=13,
    )

    with pytest.raises(CeilingExceeded) as exc:
        materialize_scenario(conn, scenario_id=sid)

    assert {b.feature_id for b in exc.value.breaches} == set(tall)
    assert set(small).isdisjoint({b.feature_id for b in exc.value.breaches})
    assert _override_count(conn, sid) == 0


# ── Concurrency ─────────────────────────────────────────────────────────────


def _edit_count(conn, scenario_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM curated_geo.scenario_edit WHERE scenario_id = %s",
            (scenario_id,),
        )
        return int(cur.fetchone()[0])


def _discard(conn, sql: str, *params) -> None:
    """One cleanup statement, guarded and committed on its own.

    Cleanup as a single transaction is cleanup that stops at its first
    failure: everything after it stays unrun and the rows this test
    *committed* live on in the session's shared database. Each statement
    therefore stands alone, and a failure is warned about rather than
    swallowed — silence here looks exactly like success.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - warned about, never fatal
        conn.rollback()
        warnings.warn(f"cleanup failed ({sql.split()[2]}): {exc}", stacklevel=2)


def test_concurrent_materialisation_does_not_revive_a_removed_edit(
    migrated_db, monkeypatch
):
    """Two materialisations of one scenario must not interleave into a third state.

    The run being held open (``c1``) resolves the scenario's edits, then pauses
    between resolution and writing. ``c2`` meanwhile removes the only edit and
    materialises that — correctly, to nothing. Without a row lock, ``c1``
    resumes with a resolution that is now historical and writes override rows
    back for an edit that no longer exists; worse, ``c2``'s digest query could
    not see those uncommitted rows, so it retires the attribute's version over
    them and the overrides end up invisible to freshness. With the lock taken
    in step 1, ``c2`` waits at its first write instead, and the end state is
    the one its (post-removal) resolution intended.

    The pause is injected rather than waited for: the window between reading
    the edits and writing the overrides is milliseconds wide on a six-block
    fixture, so a sleep-and-hope test would pass whatever the code did.

    The shared ``conn`` fixture only rolls back, so a second connection could
    not see its rows. This test commits its own setup on its own connections
    and removes it afterwards.
    """
    c1 = psycopg2.connect(migrated_db)
    c2 = psycopg2.connect(migrated_db)
    setup = psycopg2.connect(migrated_db)
    territory = None
    try:
        territory = _Territory(setup, suffix="_concurrency")
        sid = territory.scenario()
        eid = territory.edit(sid, _AREA_0_3, "num_levels", 6)
        setup.commit()

        resolved = threading.Event()
        removal_done = threading.Event()
        errors: list[BaseException] = []
        unpaused = authoring._refuse_ceiling_breaches

        def pause_after_resolution(conn, **kwargs):
            unpaused(conn, **kwargs)
            if conn is c1:  # only the run this test holds open
                resolved.set()
                # Bounded: with the lock, c2 is blocked and never arrives.
                removal_done.wait(timeout=2.0)

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
                remove_edit(c2, edit_id=eid)
                materialize_scenario(c2, scenario_id=sid)
                c2.commit()
            finally:
                removal_done.set()
        finally:
            held.join(timeout=30)

        assert not held.is_alive()
        assert not errors, errors
        # The committed scenario has no edits, so it must have no overrides.
        assert _edit_count(setup, sid) == 0
        assert _override_count(setup, sid) == 0
    finally:
        for c in (c1, c2):
            try:
                c.rollback()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"rollback failed: {exc}", stacklevel=2)
        setup.rollback()
        if territory is not None:
            _discard(
                setup,
                "DELETE FROM curated_geo.scenario_override WHERE scenario_id IN "
                "(SELECT id FROM app_core.scenario WHERE project_id = %s)",
                territory.project_id,
            )
            # Cascades scenario, scenario_edit, area_of_interest, aoi_feature.
            _discard(
                setup,
                "DELETE FROM app_core.project WHERE id = %s",
                territory.project_id,
            )
            _discard(
                setup,
                "DELETE FROM curated_geo.feature WHERE id = ANY(%s::uuid[])",
                territory.blocks,
            )
            _discard(
                setup,
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
                territory.source_name,
            )
            _discard(
                setup,
                "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
                territory.source_name,
            )
        for c in (c1, c2, setup):
            try:
                c.close()
            except Exception as exc:  # noqa: BLE001 - warned about, never fatal
                warnings.warn(f"close failed: {exc}", stacklevel=2)
