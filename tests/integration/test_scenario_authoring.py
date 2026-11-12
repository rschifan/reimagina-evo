"""Creating scenarios and adding the edits that a later step resolves to overrides."""

from __future__ import annotations

import threading
import time

import psycopg2
import pytest

from reimagina.scenarios.authoring import (
    DuplicateScenarioName,
    InvalidGeometry,
    add_edit,
    create_scenario,
    get_scenario,
    list_scenarios,
    remove_edit,
)
from tests.integration._helpers import insert_project

_SQUARE = "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))"
_BOWTIE = "POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))"


def _type_id(conn, slug: str = "block_authoring_test") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature_type_catalog
              (context, type_slug, label, geometry_family, role)
            VALUES ('MEX', %s, 'Block', 'polygon', 'analysis_unit')
            RETURNING id
            """,
            (slug,),
        )
        return str(cur.fetchone()[0])


def _aoi(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.area_of_interest (project_id, name, geometry)
            VALUES (%s, 'authoring aoi', ST_GeomFromText(%s, 4326))
            RETURNING id
            """,
            (project_id, _SQUARE),
        )
        return str(cur.fetchone()[0])


def _edit(conn, sid, type_id, wkt=_SQUARE, value=6, attribute_key="floors") -> str:
    return add_edit(
        conn,
        scenario_id=sid,
        geometry_wkt=wkt,
        type_id=type_id,
        attribute_key=attribute_key,
        value_numeric=value,
    )


def test_create_then_find_again(conn):
    p = insert_project(conn)
    a = _aoi(conn, p)
    sid = create_scenario(conn, project_id=p, name="Centralidad norte", aoi_id=a)
    assert get_scenario(conn, sid)["name"] == "Centralidad norte"
    assert [s["id"] for s in list_scenarios(conn, aoi_id=a)] == [sid]
    assert [s["id"] for s in list_scenarios(conn, project_id=p)] == [sid]


def test_an_unknown_scenario_is_none(conn):
    assert get_scenario(conn, "00000000-0000-0000-0000-000000000000") is None


def test_a_fresh_scenario_is_not_materialized_and_has_no_overrides(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    got = get_scenario(conn, sid)
    assert got["materialized_at"] is None
    assert got["override_count"] == 0
    assert got["edits"] == []


def test_duplicate_name_in_one_project_raises_its_own_error(conn):
    p = insert_project(conn)
    create_scenario(conn, project_id=p, name="N")
    with pytest.raises(DuplicateScenarioName):
        create_scenario(conn, project_id=p, name="N")
    # The failed insert must not poison the caller's transaction.
    assert create_scenario(conn, project_id=p, name="M")


def test_the_same_name_in_another_project_is_fine(conn):
    create_scenario(conn, project_id=insert_project(conn, "A"), name="N")
    create_scenario(conn, project_id=insert_project(conn, "B"), name="N")


def test_edits_get_increasing_ordinals(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    t = _type_id(conn)
    a1 = _edit(conn, sid, t, value=6)
    a2 = _edit(conn, sid, t, value=8)
    ords = {e["id"]: e["ordinal"] for e in get_scenario(conn, sid)["edits"]}
    assert ords[a2] > ords[a1]


def test_remove_edit_reports_whether_it_removed_anything(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    eid = _edit(conn, sid, _type_id(conn))
    assert remove_edit(conn, edit_id=eid) is True
    assert remove_edit(conn, edit_id=eid) is False
    assert get_scenario(conn, sid)["edits"] == []


def test_ordinals_are_not_reused_after_a_removal_of_the_latest(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    t = _type_id(conn)
    _edit(conn, sid, t)
    e2 = _edit(conn, sid, t)
    remove_edit(conn, edit_id=e2)
    e3 = _edit(conn, sid, t)
    assert [e["id"] for e in get_scenario(conn, sid)["edits"]][-1] == e3


def _is_valid(conn, eid) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_IsValid(geometry) FROM curated_geo.scenario_edit WHERE id = %s",
            (eid,),
        )
        return cur.fetchone()[0]


def test_a_self_intersecting_polygon_is_repaired_not_stored_raw(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    eid = _edit(conn, sid, _type_id(conn), wkt=_BOWTIE)
    assert _is_valid(conn, eid)


def test_an_unrepairable_geometry_is_rejected_with_a_reason(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    with pytest.raises(InvalidGeometry):
        _edit(conn, sid, _type_id(conn), wkt="POLYGON EMPTY")


def test_unparseable_wkt_is_an_invalid_geometry_too(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    t = _type_id(conn)
    with pytest.raises(InvalidGeometry):
        _edit(conn, sid, t, wkt="not wkt")
    # and the transaction is still usable
    assert _edit(conn, sid, t)


def test_a_line_is_rejected_not_stored(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    with pytest.raises(InvalidGeometry):
        _edit(conn, sid, _type_id(conn), wkt="LINESTRING(0 0, 1 1)")


def test_an_unauthorable_attribute_is_refused(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    with pytest.raises(ValueError, match="network_version"):
        _edit(conn, sid, _type_id(conn), attribute_key="network_version")


def test_an_edit_on_a_missing_scenario_is_refused(conn):
    with pytest.raises(LookupError):
        _edit(conn, "00000000-0000-0000-0000-000000000000", _type_id(conn))


def test_concurrent_add_edit_does_not_collide_on_ordinal(migrated_db):
    """Two committed transactions racing for the same scenario's next ordinal.

    The fixture ``conn`` never commits, so a second connection could not even see
    the scenario. This test commits its own setup and removes it afterwards.

    Deterministic, not lucky: the first transaction adds its edit and holds the
    scenario row lock uncommitted; the second then starts. Without the lock it
    reads the same max, inserts the same ordinal, blocks on the unique index and
    raises once the first commits. With it, it waits on the lock instead.
    """
    c1 = psycopg2.connect(migrated_db)
    c2 = psycopg2.connect(migrated_db)
    setup = psycopg2.connect(migrated_db)
    try:
        project = insert_project(setup, "concurrency")
        t = _type_id(setup, "block_authoring_concurrency")
        sid = create_scenario(setup, project_id=project, name="race")
        setup.commit()

        first = _edit(c1, sid, t, value=6)
        errors: list[BaseException] = []
        result: list[str] = []

        def second() -> None:
            try:
                result.append(_edit(c2, sid, t, value=8))
                c2.commit()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)  # let it reach the lock (or, unlocked, the unique index)
        c1.commit()
        thread.join(timeout=10)

        assert not errors, errors
        edits = get_scenario(setup, sid)["edits"]
        assert {e["id"] for e in edits} == {first, result[0]}
        assert len({e["ordinal"] for e in edits}) == 2
    finally:
        c1.rollback()
        c2.rollback()
        setup.rollback()
        with setup.cursor() as cur:
            cur.execute("DELETE FROM app_core.project WHERE name = 'concurrency'")
            cur.execute(
                "DELETE FROM curated_geo.feature_type_catalog "
                "WHERE type_slug = 'block_authoring_concurrency'"
            )
        setup.commit()
        for c in (c1, c2, setup):
            c.close()
