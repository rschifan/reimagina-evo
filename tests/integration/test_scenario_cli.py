"""The scenario CLI against a real database.

``tests/unit/test_scenario_cli.py`` pins *which* relation each command reads
and what it does with the answer; nothing there executes a statement. This
file executes them. The failure it exists to catch is a read surface that is
correct in shape and wrong in SQL — a column ``explore.scenario_v`` does not
carry, a cast Postgres will not make, a parameter name that does not match
its placeholder. Every one of those passes a monkeypatched test and fails the
first time anyone runs the command.

The CLI opens its own connection from a DSN and commits, so the setup here is
committed too, and every row is removed statement by statement with
``discard`` — the shape Tasks 5 and 6 settled on, because cleanup that stops
at its first failure leaves committed rows in a database the whole session
shares.
"""

from __future__ import annotations

import json
from uuid import uuid4

import psycopg2
import pytest

from apps.cli.app import main
from reimagina.data import aoi as aoi_mod
from tests.integration._helpers import (
    discard,
    insert_feature,
    register_source,
    start_run,
)

_SQUARE = "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))"
_TYPE_SLUG = "block_cli_test"


@pytest.fixture
def territory(migrated_db: str):
    """A committed project, AOI and feature type, removed afterwards."""
    conn = psycopg2.connect(migrated_db)
    created: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_core.project (name, description) "
                "VALUES (%s, 'scenario cli test') RETURNING id",
                (f"scenario-cli-{uuid4()}",),
            )
            created["project_id"] = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO curated_geo.area_of_interest (project_id, name, geometry)
                VALUES (%s, 'scenario cli aoi', ST_GeomFromText(%s, 4326))
                RETURNING id
                """,
                (created["project_id"], _SQUARE),
            )
            created["aoi_id"] = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO curated_geo.feature_type_catalog
                  (context, type_slug, label, geometry_family, role)
                VALUES ('MEX', %s, 'Block', 'polygon', 'analysis_unit')
                ON CONFLICT (context, type_slug) DO NOTHING
                RETURNING id
                """,
                (_TYPE_SLUG,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT id FROM curated_geo.feature_type_catalog "
                    "WHERE context = 'MEX' AND type_slug = %s",
                    (_TYPE_SLUG,),
                )
                type_id = str(cur.fetchone()[0])
            else:
                type_id = str(row[0])
        created["type_inserted"] = "yes" if row else "no"
        # One block inside the AOI: a rule is materialised as it is written,
        # and one that catches no block is refused.
        created["source"] = f"scenario-cli-src-{uuid4()}"
        source_id = register_source(
            conn,
            name=created["source"],
            storage_type="vector",
            integration_role="canonical_entity",
        )
        created["block"] = insert_feature(
            conn,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=start_run(conn, source_id),
            dataset_version="v1",
            source_record_ref="cli-blk",
            geometry_wkt="POLYGON((0.2 0.2, 0.2 0.4, 0.4 0.4, 0.4 0.2, 0.2 0.2))",
            attributes={},
        )
        aoi_mod.compute_aoi_features(conn, created["aoi_id"])
        conn.commit()
        yield created
    finally:
        discard(
            conn,
            "DELETE FROM app_core.scenario WHERE project_id = %s",
            created.get("project_id"),
        )
        discard(
            conn,
            "DELETE FROM curated_geo.area_of_interest WHERE id = %s",
            created.get("aoi_id"),
        )
        discard(
            conn, "DELETE FROM curated_geo.feature WHERE id = %s", created.get("block")
        )
        discard(
            conn,
            "DELETE FROM curated_geo.ingestion_run WHERE source_id IN "
            "(SELECT id FROM curated_geo.data_source_catalog WHERE name = %s)",
            created.get("source"),
        )
        discard(
            conn,
            "DELETE FROM curated_geo.data_source_catalog WHERE name = %s",
            created.get("source"),
        )
        discard(
            conn,
            "DELETE FROM app_core.project WHERE id = %s",
            created.get("project_id"),
        )
        if created.get("type_inserted") == "yes":
            discard(
                conn,
                "DELETE FROM curated_geo.feature_type_catalog WHERE type_slug = %s",
                _TYPE_SLUG,
            )
        conn.close()


def _run(capsys, *argv: str) -> dict:
    code = main(["scenario", *argv])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_create_add_rule_list_and_show_round_trip(
    territory, migrated_db, tmp_path, capsys
):
    """One pass over every statement the commands send.

    The assertions are about the *view*: a scenario the CLI created is listed
    by ``explore.scenario_v`` with the counts that view computes. A rule is
    one row and is materialised as it is written, so its overrides are there
    at once — only the numbers it was given, no default coverage.
    """
    created = _run(
        capsys,
        "create",
        "--name",
        "Centralidad norte",
        "--aoi-id",
        territory["aoi_id"],
        "--dsn",
        migrated_db,
    )
    # The project was derived from the AOI, not asked for.
    assert created["project_id"] == territory["project_id"]
    scenario_id = created["scenario_id"]

    listed = _run(capsys, "list", "--aoi-id", territory["aoi_id"], "--dsn", migrated_db)
    assert [s["id"] for s in listed["scenarios"]] == [scenario_id]
    assert listed["scenarios"][0]["rule_count"] == 0
    assert listed["scenarios"][0]["override_count"] == 0
    assert listed["scenarios"][0]["materialized_at"] is None

    area = tmp_path / "area.geojson"
    area.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
                        },
                    }
                ],
            }
        )
    )
    written = _run(
        capsys,
        "add-rule",
        "--scenario-id",
        scenario_id,
        "--geojson",
        str(area),
        "--levels",
        "6",
        "--people-per-household",
        "3.6",
        "--type-slug",
        _TYPE_SLUG,
        "--dsn",
        migrated_db,
    )
    assert written["inputs"] == {"levels": 6.0, "people_per_household": 3.6}
    assert written["materialized"]["matched_per_rule"] == {written["rule_id"]: 1}

    shown = _run(capsys, "show", "--scenario-id", scenario_id, "--dsn", migrated_db)
    assert shown["scenario"]["rule_count"] == 1
    assert shown["scenario"]["name"] == "Centralidad norte"
    assert shown["scenario"]["materialized_at"] is not None
    assert shown["scenario"]["override_count"] == 3  # floors, num_levels, people

    rule_id = written["rule_id"]
    _run(
        capsys,
        "update-rule",
        "--scenario-id",
        scenario_id,
        "--rule-id",
        rule_id,
        "--levels",
        "4",
        "--dsn",
        migrated_db,
    )
    rules = _run(capsys, "rules", "--scenario-id", scenario_id, "--dsn", migrated_db)
    assert [(r["id"], r["inputs"]) for r in rules["rules"]] == [
        (rule_id, {"levels": 4.0})
    ]

    _run(
        capsys,
        "delete-rule",
        "--scenario-id",
        scenario_id,
        "--rule-id",
        rule_id,
        "--dsn",
        migrated_db,
    )
    shown = _run(capsys, "show", "--scenario-id", scenario_id, "--dsn", migrated_db)
    assert (shown["scenario"]["rule_count"], shown["scenario"]["override_count"]) == (
        0,
        0,
    )

    deleted = _run(capsys, "delete", "--scenario-id", scenario_id, "--dsn", migrated_db)
    assert deleted["scenario_id"] == scenario_id
    assert (
        main(["scenario", "show", "--scenario-id", scenario_id, "--dsn", migrated_db])
        == 2
    )


def test_a_malformed_id_is_a_refusal_not_a_traceback(migrated_db, capsys):
    """SC-10: the id used to reach Postgres as text and raise a DataError."""
    code = main(
        ["scenario", "recompute", "--scenario-id", "not-a-uuid", "--dsn", migrated_db]
    )

    assert code == 2
    assert "not-a-uuid" in capsys.readouterr().err


def test_list_narrows_to_the_project_it_was_given(territory, migrated_db, capsys):
    """The `::uuid IS NULL OR …` filters have to work in Postgres, not only
    in a recorded parameter dict."""
    _run(
        capsys,
        "create",
        "--name",
        "Narrowing",
        "--aoi-id",
        territory["aoi_id"],
        "--dsn",
        migrated_db,
    )
    other = str(uuid4())

    assert (
        _run(capsys, "list", "--project-id", other, "--dsn", migrated_db)["count"] == 0
    )
    mine = _run(
        capsys, "list", "--project-id", territory["project_id"], "--dsn", migrated_db
    )
    assert [s["name"] for s in mine["scenarios"]] == ["Narrowing"]


def test_show_refuses_an_id_that_matches_nothing(migrated_db, capsys):
    code = main(
        ["scenario", "show", "--scenario-id", str(uuid4()), "--dsn", migrated_db]
    )

    assert code == 2
    assert "no scenario with id" in capsys.readouterr().err


def test_create_refuses_a_duplicate_name_in_the_same_project(
    territory, migrated_db, capsys
):
    """`UNIQUE (project_id, name)` reaches the user as a message, not a 500.

    The savepoint in `create_scenario` is what makes the second call a clean
    refusal; this is the CLI end of it.
    """
    argv = [
        "scenario",
        "create",
        "--name",
        "Twice",
        "--aoi-id",
        territory["aoi_id"],
        "--dsn",
        migrated_db,
    ]
    assert main(argv) == 0
    capsys.readouterr()

    assert main(argv) == 2
    assert "already exists" in capsys.readouterr().err
