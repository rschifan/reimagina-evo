"""Creating scenarios and adding the rules that a later step resolves to overrides."""

from __future__ import annotations

import threading
import time

import psycopg2
import pytest

from reimagina.scenarios.authoring import (
    DuplicateScenarioName,
    InvalidGeometry,
    RuleNotFound,
    ScenarioNotFound,
    add_rule,
    create_scenario,
    delete_rule,
    get_scenario,
    list_scenarios,
    update_rule,
)
from reimagina.scenarios.proposal import ProposalValueError
from reimagina.data.aoi import compute_aoi_features
from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

_SQUARE = "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))"
_BOWTIE = "POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))"


#: A block whose interior point every drawn area below covers — the square,
#: the bowtie's left half and the Z triangle alike — so a rule catches it.
_BLOCK = "POLYGON((0.199 0.599, 0.201 0.599, 0.201 0.601, 0.199 0.601, 0.199 0.599))"


def _type_id(conn, slug: str = "block_authoring_test") -> str:
    """A feature type with one block of it, inside every test AOI.

    A rule is materialised as it is written and refused if it catches no
    block, so authoring needs something to catch. Call it before ``_scenario``,
    which computes the AOI's membership.
    """
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
        type_id = str(cur.fetchone()[0])
    source_id = register_source(
        conn,
        name=f"src_{slug}",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    insert_feature(
        conn,
        type_id=type_id,
        source_id=source_id,
        ingestion_run_id=start_run(conn, source_id),
        dataset_version="v1",
        source_record_ref=f"blk-{slug}",
        geometry_wkt=_BLOCK,
    )
    return type_id


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
        aoi_id = str(cur.fetchone()[0])
    compute_aoi_features(conn, aoi_id)
    return aoi_id


def _scenario(conn, name: str = "N", project_id: str | None = None) -> str:
    """A scenario a rule can be drawn on: in a project, over an AOI."""
    project_id = project_id or insert_project(conn)
    return create_scenario(
        conn, project_id=project_id, name=name, aoi_id=_aoi(conn, project_id)
    )


def _rule(conn, sid, type_id, wkt=_SQUARE, **inputs) -> str:
    rule, _ = add_rule(
        conn,
        scenario_id=sid,
        geometry_wkt=wkt,
        type_id=type_id,
        inputs=inputs or {"levels": 6},
    )
    return rule["id"]


def test_create_then_find_again(conn):
    p = insert_project(conn)
    a = _aoi(conn, p)
    sid = create_scenario(conn, project_id=p, name="Centralidad norte", aoi_id=a)
    assert get_scenario(conn, sid)["name"] == "Centralidad norte"
    assert [s["id"] for s in list_scenarios(conn, aoi_id=a)] == [sid]
    assert [s["id"] for s in list_scenarios(conn, project_id=p)] == [sid]


def _run(conn, sid, status, submitted_at):
    """One run envelope — what the Runs page lists and `GET /jobs/{id}` reads."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.orchestration_run "
            "(scenario_id, status, code_version, submitted_at) "
            "VALUES (%s, %s, 'test', %s::timestamptz) RETURNING id",
            (sid, status, submitted_at),
        )
        return str(cur.fetchone()[0])


def _unit_provenance(conn, sid, status, queued_at):
    """A per-indicator `job_run` row: provenance of one unit, not a run."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_runtime.job_run "
            "(model_family, status, code_version, scenario_id, queued_at) "
            "VALUES ('test', %s, 'test', %s, %s::timestamptz)",
            (status, sid, queued_at),
        )


def test_the_list_counts_each_scenarios_rules(conn):
    """The workspace says "no rules yet" from the list, not one read per row."""
    p = insert_project(conn)
    t = _type_id(conn)
    a = _aoi(conn, p)
    with_rules = create_scenario(conn, project_id=p, name="with", aoi_id=a)
    create_scenario(conn, project_id=p, name="without", aoi_id=a)
    _rule(conn, with_rules, t, levels=6, unit_size=60)
    _rule(conn, with_rules, t, people_per_household=3)

    rows = {r["name"]: r for r in list_scenarios(conn, aoi_id=a)}

    assert rows["with"]["rule_count"] == 2
    assert rows["without"]["rule_count"] == 0


def test_the_list_names_each_scenarios_newest_run(conn):
    """A failed recompute still sets materialized_at; only its run says it failed.

    The run is the envelope in `orchestration_run`, not a `job_run` row:
    those are written one per indicator unit, so the newest of them can be a
    unit that succeeded inside a run that failed — and its id is not a run id
    `GET /jobs/{run_id}` knows.
    """
    p = insert_project(conn)
    a = _aoi(conn, p)
    ran = create_scenario(conn, project_id=p, name="A", aoi_id=a)
    create_scenario(conn, project_id=p, name="B", aoi_id=a)
    _run(conn, ran, "succeeded", "2026-11-18T09:00:00Z")
    newest = _run(conn, ran, "partial", "2026-11-18T10:00:00Z")
    _unit_provenance(conn, ran, "succeeded", "2026-11-18T11:00:00Z")

    rows = {r["name"]: r for r in list_scenarios(conn, aoi_id=a)}

    assert rows["A"]["latest_run"] == {"id": newest, "status": "partial"}
    assert rows["B"]["latest_run"] is None


def test_an_unknown_scenario_is_none(conn):
    assert get_scenario(conn, "00000000-0000-0000-0000-000000000000") is None


def test_a_fresh_scenario_is_not_materialized_and_has_no_overrides(conn):
    sid = create_scenario(conn, project_id=insert_project(conn), name="N")
    got = get_scenario(conn, sid)
    assert got["materialized_at"] is None
    assert got["override_count"] == 0
    assert got["rules"] == []


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


def test_a_scenario_cannot_be_bounded_by_another_projects_aoi(conn):
    """⚠ The cross-tenant hole r048's schema leaves open.

    Both FKs are satisfied by this pair — the project exists, the AOI exists —
    and nothing in the schema relates them, so the insert would be accepted and
    ``materialize_scenario``, which scopes selection by ``aoi_id`` alone, would
    resolve overrides over the *other* project's features.

    A cross-table claim, so it needs real tables: no contract-layer double can
    tell a mismatched pair from an agreeing one.
    """
    mine = insert_project(conn, "tenant A")
    theirs = insert_project(conn, "tenant B")
    their_aoi = _aoi(conn, theirs)

    with pytest.raises(ScenarioNotFound):
        create_scenario(conn, project_id=mine, name="Cross-tenant", aoi_id=their_aoi)

    # Refused, not merely reported: nothing was written for my project.
    assert list_scenarios(conn, project_id=mine) == []
    # The live control — the same AOI, under the project that owns it. Without
    # it, a guard that refused every AOI would pass the assertion above.
    assert create_scenario(
        conn, project_id=theirs, name="Same tenant", aoi_id=their_aoi
    )


def test_an_aoi_that_exists_nowhere_is_still_the_foreign_keys_refusal(conn):
    """The guard above must not swallow the unknown-AOI case.

    ``ForeignKeyViolation`` is what the API turns into a 404 naming the field
    it was about (read off ``diag.constraint_name``); a ``ScenarioNotFound``
    from the guard would answer with a bare message instead and quietly
    downgrade that. Last assertion in this test on purpose: the violation
    aborts the transaction.
    """
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        create_scenario(
            conn,
            project_id=insert_project(conn),
            name="N",
            aoi_id="00000000-0000-0000-0000-000000000000",
        )


def test_rules_get_increasing_ordinals(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    a1 = _rule(conn, sid, t, levels=6)
    a2 = _rule(conn, sid, t, levels=8)
    ords = {r["id"]: r["ordinal"] for r in get_scenario(conn, sid)["rules"]}
    assert ords[a2] > ords[a1]


def test_a_rule_stores_only_the_numbers_set_and_storeys_once(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    _rule(conn, sid, t, levels=4, lot_coverage=0.6)
    (rule,) = get_scenario(conn, sid)["rules"]
    assert rule["inputs"] == {"levels": 4.0, "lot_coverage": 0.6}


def test_updating_a_rule_keeps_its_id_and_its_place(conn):
    """Re-adding a number a rule had dropped does not move it ahead of a
    later rule: precedence is the rule's ordinal, assigned once (B5)."""
    t = _type_id(conn)
    sid = _scenario(conn)
    first = _rule(conn, sid, t, levels=4, unit_size=90)
    second = _rule(conn, sid, t, unit_size=60)
    update_rule(
        conn, scenario_id=sid, rule_id=first, geometry_wkt=None, inputs={"levels": 4}
    )
    update_rule(
        conn,
        scenario_id=sid,
        rule_id=first,
        geometry_wkt=None,
        inputs={"levels": 4, "unit_size": 90},
    )

    rules = get_scenario(conn, sid)["rules"]
    assert [r["id"] for r in rules] == [first, second]
    assert rules[0]["inputs"] == {"levels": 4.0, "unit_size": 90.0}


def test_a_merged_update_keeps_the_numbers_it_does_not_name(conn):
    """The CLI's ``update-rule``: merged under the scenario's lock, and the
    row handed back is the stored one."""
    t = _type_id(conn)
    sid = _scenario(conn)
    rid = _rule(conn, sid, t, levels=4, unit_size=90)

    rule, result = update_rule(
        conn,
        scenario_id=sid,
        rule_id=rid.upper(),
        geometry_wkt=None,
        inputs={"people_per_household": 3, "levels": None},
        unset=["unit_size"],
        merge=True,
    )

    assert rule["id"] == rid
    assert rule["inputs"] == {"levels": 4.0, "people_per_household": 3.0}
    assert get_scenario(conn, sid)["rules"] == [rule]
    assert result.matched_per_rule == {rid: 1}


def test_a_malformed_rule_id_names_no_rule(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    _rule(conn, sid, t)
    with pytest.raises(RuleNotFound, match="no rule"):
        delete_rule(conn, scenario_id=sid, rule_id="not-a-uuid")


def test_a_rule_of_another_scenario_cannot_be_changed_through_this_one(conn):
    p = insert_project(conn)
    t = _type_id(conn)
    mine = _scenario(conn, "mine", p)
    theirs = _scenario(conn, "theirs", p)
    their_rule = _rule(conn, theirs, t)

    with pytest.raises(RuleNotFound):
        update_rule(
            conn,
            scenario_id=mine,
            rule_id=their_rule,
            geometry_wkt=None,
            inputs={"levels": 9},
        )
    with pytest.raises(RuleNotFound):
        delete_rule(conn, scenario_id=mine, rule_id=their_rule)
    assert get_scenario(conn, theirs)["rules"][0]["inputs"] == {"levels": 6.0}


def test_deleting_a_rule_is_one_row_and_a_second_delete_is_refused(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    rid = _rule(conn, sid, t, levels=6, unit_size=60)
    delete_rule(conn, scenario_id=sid, rule_id=rid)
    with pytest.raises(RuleNotFound):
        delete_rule(conn, scenario_id=sid, rule_id=rid)
    assert get_scenario(conn, sid)["rules"] == []


def test_ordinals_are_not_reused_after_a_removal_of_the_latest(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    _rule(conn, sid, t)
    r2 = _rule(conn, sid, t)
    delete_rule(conn, scenario_id=sid, rule_id=r2)
    r3 = _rule(conn, sid, t)
    assert [r["id"] for r in get_scenario(conn, sid)["rules"]][-1] == r3


def _is_valid(conn, rid) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_IsValid(geometry) FROM curated_geo.scenario_rule WHERE id = %s",
            (rid,),
        )
        return cur.fetchone()[0]


def test_a_self_intersecting_polygon_is_repaired_not_stored_raw(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    rid = _rule(conn, sid, t, wkt=_BOWTIE)
    assert _is_valid(conn, rid)


def test_a_third_coordinate_is_dropped_not_a_500(conn):
    """QGIS and GPS exports carry Z; the column is 2D and the insert raised."""
    t = _type_id(conn)
    sid = _scenario(conn)
    rid = _rule(conn, sid, t, wkt="POLYGON Z((0 0 5,0 1 5,1 1 5,0 0 5))")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_NDims(geometry) FROM curated_geo.scenario_rule WHERE id = %s",
            (rid,),
        )
        assert cur.fetchone()[0] == 2


def test_an_unrepairable_geometry_is_rejected_with_a_reason(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    with pytest.raises(InvalidGeometry):
        _rule(conn, sid, t, wkt="POLYGON EMPTY")


def test_unparseable_wkt_is_an_invalid_geometry_too(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    with pytest.raises(InvalidGeometry):
        _rule(conn, sid, t, wkt="not wkt")
    # and the transaction is still usable
    assert _rule(conn, sid, t)


def test_a_line_is_rejected_not_stored(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    with pytest.raises(InvalidGeometry):
        _rule(conn, sid, t, wkt="LINESTRING(0 0, 1 1)")


def test_an_unknown_number_is_refused(conn):
    t = _type_id(conn)
    sid = _scenario(conn)
    with pytest.raises(ProposalValueError, match="network_version"):
        _rule(conn, sid, t, network_version=3)


def test_a_rule_on_a_missing_scenario_is_refused(conn):
    with pytest.raises(LookupError):
        _rule(conn, "00000000-0000-0000-0000-000000000000", _type_id(conn))


def test_concurrent_add_rule_does_not_collide_on_ordinal(migrated_db):
    """Two committed transactions racing for the same scenario's next ordinal.

    The fixture ``conn`` never commits, so a second connection could not even see
    the scenario. This test commits its own setup and removes it afterwards.

    Deterministic, not lucky: the first transaction adds its rule and holds the
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
        sid = _scenario(setup, "race", project)
        setup.commit()

        first = _rule(c1, sid, t, levels=6)
        errors: list[BaseException] = []
        result: list[str] = []

        def second() -> None:
            try:
                result.append(_rule(c2, sid, t, levels=8))
                c2.commit()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)  # let it reach the lock (or, unlocked, the unique index)
        c1.commit()
        thread.join(timeout=10)

        assert not errors, errors
        rules = get_scenario(setup, sid)["rules"]
        assert {r["id"] for r in rules} == {first, result[0]}
        assert len({r["ordinal"] for r in rules}) == 2
    finally:
        c1.rollback()
        c2.rollback()
        setup.rollback()
        with setup.cursor() as cur:
            # Cascades scenario, rules, overrides, AOI and its membership.
            cur.execute("DELETE FROM app_core.project WHERE name = 'concurrency'")
            cur.execute(
                "DELETE FROM curated_geo.feature WHERE type_id IN (SELECT id "
                "FROM curated_geo.feature_type_catalog "
                "WHERE type_slug = 'block_authoring_concurrency')"
            )
            cur.execute(
                "DELETE FROM curated_geo.feature_type_catalog "
                "WHERE type_slug = 'block_authoring_concurrency'"
            )
            cur.execute(
                "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
                "(SELECT id FROM curated_geo.data_source_catalog "
                "WHERE name = 'src_block_authoring_concurrency')"
            )
            cur.execute(
                "DELETE FROM curated_geo.data_source_catalog "
                "WHERE name = 'src_block_authoring_concurrency'"
            )
        setup.commit()
        for c in (c1, c2, setup):
            c.close()
