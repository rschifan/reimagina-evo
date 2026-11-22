"""The scenario tables: constraints, row-level security, and the explore view.

Catalog-level assertions (unique / FK / RLS / policy) read ``pg_catalog`` rather
than trying the violation, because the ``conn`` fixture never commits and a
constraint that was declared but is not the one intended would still
reject *something*.
"""

from __future__ import annotations

import psycopg2.errors
import pytest

from tests.integration._helpers import (
    insert_feature,
    insert_project,
    register_source,
    start_run,
)

_SQUARE = "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))"


def _split(qualified: str) -> tuple[str, str]:
    schema, table = qualified.split(".")
    return schema, table


def _has_unique(conn, qualified: str, columns: list[str]) -> bool:
    schema, table = _split(qualified)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT array_agg(a.attname::text ORDER BY k.ord)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE c.contype = 'u' AND n.nspname = %s AND t.relname = %s
            GROUP BY c.oid
            """,
            (schema, table),
        )
        return columns in [row[0] for row in cur.fetchall()]


def _has_fk(conn, qualified: str, column: str, references: str) -> bool:
    schema, table = _split(qualified)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f' AND n.nspname = %s AND t.relname = %s
              AND a.attname = %s AND c.confrelid = %s::regclass
            """,
            (schema, table, column, references),
        )
        return cur.fetchone() is not None


def _rls_enabled(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def _policy_named(conn, schema: str, table: str, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_policies "
            "WHERE schemaname = %s AND tablename = %s AND policyname = %s",
            (schema, table, name),
        )
        return cur.fetchone() is not None


def _column_exists(conn, qualified: str, column: str) -> bool:
    schema, table = _split(qualified)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (schema, table, column),
        )
        return cur.fetchone() is not None


def _column_not_null(conn, qualified: str, column: str) -> bool:
    schema, table = _split(qualified)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
            (schema, table, column),
        )
        row = cur.fetchone()
        return row is not None and row[0] == "NO"


def _insert_scenario(conn, project_id: str, name: str = "densify") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_core.scenario (project_id, name) "
            "VALUES (%s, %s) RETURNING id",
            (project_id, name),
        )
        return str(cur.fetchone()[0])


def _block_type_id(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.feature_type_catalog
              (context, type_slug, label, geometry_family, role)
            VALUES ('MEX', 'block_scenario_schema_test', 'Block', 'polygon',
                    'analysis_unit')
            RETURNING id
            """
        )
        return str(cur.fetchone()[0])


def _insert_rule(
    conn,
    scenario_id: str,
    type_id: str,
    *,
    ordinal: int = 1,
    geometry_wkt: str = _SQUARE,
    inputs: str = '{"levels": 6}',
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO curated_geo.scenario_rule
              (scenario_id, ordinal, geometry, type_id, inputs)
            VALUES (%s, %s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s, %s::jsonb)
            RETURNING id
            """,
            (scenario_id, ordinal, geometry_wkt, type_id, inputs),
        )
        return str(cur.fetchone()[0])


def test_scenario_tables_exist_with_their_constraints(conn):
    # app_core.scenario
    assert _has_unique(conn, "app_core.scenario", ["project_id", "name"])
    assert _has_fk(conn, "app_core.scenario", "project_id", "app_core.project")
    assert _column_exists(conn, "app_core.scenario", "materialized_at")
    assert _rls_enabled(conn, "app_core", "scenario")
    assert _policy_named(conn, "app_core", "scenario", "tenant_isolation")
    # curated_geo.scenario_rule (r055)
    assert _has_unique(conn, "curated_geo.scenario_rule", ["scenario_id", "ordinal"])
    assert _has_fk(
        conn, "curated_geo.scenario_rule", "type_id", "curated_geo.feature_type_catalog"
    )
    assert _has_fk(
        conn, "curated_geo.scenario_rule", "scenario_id", "app_core.scenario"
    )
    assert _column_not_null(conn, "curated_geo.scenario_rule", "inputs")
    # r048's per-attribute edits are gone: a rule is one row.
    assert not _column_exists(conn, "curated_geo.scenario_edit", "id")


@pytest.mark.parametrize(
    "table",
    [
        "curated_geo.scenario_override",
        "curated_geo.indicator_result",
        "jobs_runtime.job_run",
        "curated_geo.aoi_indicator_summary",
    ],
)
def test_every_scenario_id_column_references_the_scenario(conn, table):
    """r016 and r030 left these bare uuids; r055 made them FKs that cascade, so
    deleting a scenario (or a project) cannot orphan them, and a late write for
    a deleted scenario fails instead of landing."""
    assert _has_fk(conn, table, "scenario_id", "app_core.scenario")
    schema, name = _split(table)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.confdeltype FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE c.contype = 'f' AND n.nspname = %s AND t.relname = %s
              AND c.confrelid = 'app_core.scenario'::regclass
            """,
            (schema, name),
        )
        assert cur.fetchone()[0] == "c"  # ON DELETE CASCADE


def test_scenario_rule_rejects_a_non_polygon_geometry(conn):
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_rule(conn, sid, type_id, geometry_wkt="LINESTRING(0 0, 1 1)")


def test_scenario_rule_rejects_an_empty_set_of_numbers(conn):
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_rule(conn, sid, type_id, inputs="{}")


def test_scenario_rule_ordinal_is_unique_per_scenario(conn):
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    _insert_rule(conn, sid, type_id, ordinal=1)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_rule(conn, sid, type_id, ordinal=1)


def test_tenant_policy_mirrors_the_project_scoped_predicate(conn):
    """Same permissive-when-unset shape as r011's policies, keyed on project_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT qual FROM pg_policies "
            "WHERE schemaname = 'app_core' AND tablename = 'scenario' "
            "AND policyname = 'tenant_isolation'"
        )
        qual = cur.fetchone()[0]
        cur.execute(
            "SELECT qual FROM pg_policies "
            "WHERE schemaname = 'curated_geo' AND tablename = 'area_of_interest' "
            "AND policyname = 'tenant_isolation'"
        )
        aoi_qual = cur.fetchone()[0]
    assert qual == aoi_qual


def test_explore_exposes_scenarios_with_their_counts(conn):
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    _insert_rule(conn, sid, type_id, ordinal=1)
    _insert_rule(conn, sid, type_id, ordinal=2)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, rule_count, override_count, aoi_id, materialized_at "
            "FROM explore.scenario_v WHERE id = %s",
            (sid,),
        )
        name, rules, overrides, aoi_id, materialized_at = cur.fetchone()
    assert name == "densify"
    assert rules == 2
    assert overrides == 0
    assert aoi_id is None and materialized_at is None


def test_explore_override_count_counts_the_scenarios_overrides(conn):
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    source_id = register_source(
        conn,
        name="scn-schema",
        storage_type="vector",
        integration_role="canonical_entity",
    )
    run_id = start_run(conn, source_id)
    features = [
        insert_feature(
            conn,
            type_id=type_id,
            source_id=source_id,
            ingestion_run_id=run_id,
            dataset_version="v1",
            source_record_ref=f"b{i}",
            geometry_wkt=_SQUARE,
        )
        for i in range(3)
    ]
    with conn.cursor() as cur:
        for fid in features:
            cur.execute(
                "INSERT INTO curated_geo.scenario_override "
                "(scenario_id, feature_id, attribute_key, value_numeric) "
                "VALUES (%s, %s, 'floors', 6)",
                (sid, fid),
            )
        cur.execute(
            "SELECT override_count FROM explore.scenario_v WHERE id = %s", (sid,)
        )
        assert cur.fetchone()[0] == 3


# ── r055's conversion of r048's edits into rules ─────────────────────────


def _r055():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "infra/db/migrations/versions/r055_scenario_rule.py"
    )
    spec = importlib.util.spec_from_file_location("r055", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r055_groups_edits_into_rules_and_back(conn):
    """The upgrade's data step, run against a hand-made r048 ``scenario_edit``.

    Edits group by identical geometry, as the workspace grouped them; a rule's
    ordinal is the rank of its first edit; storeys come from ``num_levels``
    (``floors`` for a half-written rule); the latest edit of a key wins. The
    downgrade's re-expansion keeps the rules' order.
    """
    r055 = _r055()
    sid = _insert_scenario(conn, insert_project(conn))
    type_id = _block_type_id(conn)
    other = "POLYGON((2 2, 2 3, 3 3, 3 2, 2 2))"
    with conn.cursor() as cur:
        cur.execute(r055._SCENARIO_EDIT_DDL)
        for ordinal, wkt, key, value in [
            (3, _SQUARE, "floors", 4),
            (4, _SQUARE, "num_levels", 4),
            (5, _SQUARE, "unit_size", 90),
            (6, other, "floors", 2),  # half-written: floors only
            (7, other, "people_per_household", 3),
            (9, _SQUARE, "lot_coverage", 0.6),  # re-added later: same rule
        ]:
            cur.execute(
                "INSERT INTO curated_geo.scenario_edit "
                "(scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric) "
                "VALUES (%s, %s, ST_GeomFromText(%s, 4326), %s, %s, %s)",
                (sid, ordinal, wkt, type_id, key, value),
            )
        cur.execute(r055.CONVERT_EDITS)
        cur.execute(
            "SELECT ordinal, inputs, ST_AsText(geometry) FROM curated_geo.scenario_rule "
            "WHERE scenario_id = %s ORDER BY ordinal",
            (sid,),
        )
        rules = cur.fetchall()
        assert [(o, i) for o, i, _ in rules] == [
            (1, {"levels": 4, "unit_size": 90, "lot_coverage": 0.6}),
            (2, {"levels": 2, "people_per_household": 3}),
        ]

        cur.execute("DELETE FROM curated_geo.scenario_edit")
        cur.execute(r055.EXPAND_RULES)
        cur.execute(
            "SELECT attribute_key, value_numeric FROM curated_geo.scenario_edit "
            "WHERE scenario_id = %s ORDER BY ordinal",
            (sid,),
        )
        assert cur.fetchall() == [
            ("floors", 4),
            ("num_levels", 4),
            ("unit_size", 90),
            ("lot_coverage", 0.6),
            ("floors", 2),
            ("num_levels", 2),
            ("people_per_household", 3),
        ]
