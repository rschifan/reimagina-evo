"""Scenario rules as rows, and the scenario id as a foreign key.

r048 stored a scenario's rules as ``curated_geo.scenario_edit``: one row per
attribute, each with its own ordinal and its own copy of the area. A "rule"
existed only as "edits whose geometry is byte-identical", so removing one was
N separate deletes that could half-fail, two rules drawn over the same area
merged, and re-adding one number gave that attribute alone a new ordinal —
which silently reordered precedence for it and nothing else.

``curated_geo.scenario_rule``
    One drawn area and the numbers the planner set over it (``inputs``:
    ``levels``, ``unit_size``, ``people_per_household``, ``lot_coverage``;
    a number left out is not set). Storeys are stored once, as ``levels``;
    materialisation writes them to both ``floors`` and ``num_levels``.
    ``ordinal`` is assigned when the rule is created and never changes after:
    editing a rule's numbers or area keeps its place in "later wins".
    There is no state column. A rule the server would refuse to materialise
    is never stored: the write and the materialisation share one transaction.

    Existing edits are grouped as the workspace grouped them (scenario, type,
    identical geometry); a group's ordinal is the rank of its first edit and
    each number is the one its latest edit carried. ``scenario_edit`` is then
    dropped.

The scenario id becomes a foreign key, ``ON DELETE CASCADE``, on
``scenario_override``, ``indicator_result``, ``jobs_runtime.job_run`` and
``aoi_indicator_summary`` — r016 and r030 left it a bare uuid while the entity
was deferred, and r048 added the entity without going back. They are
``DEFERRABLE INITIALLY IMMEDIATE``: immediate for everyone, deferred by a
compute unit (``runners._fenced``) so its inserts do not hold a lock on the
scenario row — and with it ``DELETE /scenarios/{id}`` — for the whole compute. Rows naming a
scenario that does not exist are deleted first: nothing could read them, and
the constraint could not be added over them. ``orchestration_run`` keeps no FK
(it is run history), and asset versions and materialisations are keyed by the
partition string ``scenario=<id>``, which no FK can follow; ``delete_scenario``
still deletes those by hand, and their orphans are deleted here too.

The downgrade re-expands every rule into r048's edits (``levels`` into
``floors`` and ``num_levels``), keeping the rules' order, and drops the FKs.

Revision ID: r055
Revises: r054
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "r055"
down_revision = "r054"
branch_labels = None
depends_on = None

#: (table, constraint name) for every scenario_id column that gains an FK.
_SCENARIO_FKS = (
    ("curated_geo.scenario_override", "scenario_override_scenario_id_fkey"),
    ("curated_geo.indicator_result", "indicator_result_scenario_id_fkey"),
    ("jobs_runtime.job_run", "job_run_scenario_id_fkey"),
    ("curated_geo.aoi_indicator_summary", "aoi_indicator_summary_scenario_id_fkey"),
)

#: The inputs a rule carries, and the edit attribute each was stored under.
#: ``levels`` came from ``num_levels`` (or, for a half-written rule, ``floors``).
_INPUT_KEYS = (
    ("levels", "num_levels"),
    ("unit_size", "unit_size"),
    ("people_per_household", "people_per_household"),
    ("lot_coverage", "lot_coverage"),
)


def _latest(attribute: str) -> str:
    return (
        f"(array_agg(value_numeric ORDER BY ordinal DESC) "
        f"FILTER (WHERE attribute_key = '{attribute}'))[1]"
    )


#: Edits → rules. Public to the tests, which run it against a hand-made
#: ``scenario_edit`` inside a rolled-back transaction.
CONVERT_EDITS = f"""
    INSERT INTO curated_geo.scenario_rule
      (scenario_id, ordinal, type_id, geometry, inputs, created_at, updated_at)
    SELECT scenario_id,
           row_number() OVER (PARTITION BY scenario_id ORDER BY first_ordinal),
           type_id, geometry, inputs, created_at, created_at
    FROM (
        SELECT scenario_id, type_id,
               (array_agg(geometry))[1] AS geometry,
               min(ordinal) AS first_ordinal,
               min(created_at) AS created_at,
               jsonb_strip_nulls(jsonb_build_object(
                   'levels', COALESCE({_latest("num_levels")}, {_latest("floors")}),
                   'unit_size', {_latest("unit_size")},
                   'people_per_household', {_latest("people_per_household")},
                   'lot_coverage', {_latest("lot_coverage")}
               )) AS inputs
        FROM curated_geo.scenario_edit
        GROUP BY scenario_id, type_id, ST_AsEWKB(geometry)
    ) grouped
"""

#: Rules → edits, for the downgrade. Ten ordinals per rule keep the rules'
#: order and give each of its (at most five) edits its own.
EXPAND_RULES = """
    INSERT INTO curated_geo.scenario_edit
      (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric,
       created_at)
    SELECT r.scenario_id, r.ordinal * 10 + k.n, r.geometry, r.type_id,
           k.attribute_key, (r.inputs ->> k.input)::double precision, r.created_at
    FROM curated_geo.scenario_rule r
    CROSS JOIN (VALUES (1, 'levels', 'floors'),
                       (2, 'levels', 'num_levels'),
                       (3, 'unit_size', 'unit_size'),
                       (4, 'people_per_household', 'people_per_household'),
                       (5, 'lot_coverage', 'lot_coverage'))
               AS k(n, input, attribute_key)
    WHERE r.inputs ? k.input
"""

#: Versions and materialisations of a partition ``scenario=<id>`` whose
#: scenario no longer exists (``assets.keys.partition_key`` renders dimensions
#: sorted, ``name=value``, joined by ``/``). No FK can follow the string, so a
#: scenario deleted before ``delete_scenario`` removed them left them behind,
#: and ``asset_component_v`` keeps such a partition a node in the graph that
#: ``assets stale`` lists for ever. Public to the tests.
DELETE_ORPHAN_PARTITIONS = tuple(
    f"""
    DELETE FROM curated_geo.{table} t
    WHERE t.partition_key ~ '(^|/)scenario='
      AND NOT EXISTS (
          SELECT 1 FROM app_core.scenario s
          WHERE s.id::text
                = substring(t.partition_key from '(?:^|/)scenario=([^/]*)'))
    """
    for table in ("asset_materialization", "asset_version")
)

_SCENARIO_EDIT_DDL = """
    CREATE TABLE curated_geo.scenario_edit (
        id            uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
        scenario_id   uuid             NOT NULL
                                       REFERENCES app_core.scenario(id) ON DELETE CASCADE,
        ordinal       integer          NOT NULL,
        geometry      geometry(Geometry, 4326) NOT NULL
                      CHECK (ST_GeometryType(geometry)
                             IN ('ST_Polygon', 'ST_MultiPolygon')),
        type_id       uuid             NOT NULL
                                       REFERENCES curated_geo.feature_type_catalog(id),
        attribute_key text             NOT NULL,
        value_numeric double precision NOT NULL,
        created_at    timestamptz      NOT NULL DEFAULT now(),
        UNIQUE (scenario_id, ordinal)
    )
"""


def _scenario_view(count_name: str, count_table: str) -> str:
    return f"""
        CREATE VIEW explore.scenario_v AS
        SELECT
            s.id,
            s.project_id,
            s.name,
            s.description,
            s.aoi_id,
            (SELECT count(*) FROM {count_table} e
              WHERE e.scenario_id = s.id)     AS {count_name},
            (SELECT count(*) FROM curated_geo.scenario_override o
              WHERE o.scenario_id = s.id)     AS override_count,
            s.materialized_at,
            s.created_at,
            s.updated_at
        FROM app_core.scenario s
    """


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.scenario_rule (
            id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id uuid        NOT NULL
                                    REFERENCES app_core.scenario(id) ON DELETE CASCADE,
            ordinal     integer     NOT NULL,
            type_id     uuid        NOT NULL
                                    REFERENCES curated_geo.feature_type_catalog(id),
            geometry    geometry(Geometry, 4326) NOT NULL
                        CHECK (ST_GeometryType(geometry)
                               IN ('ST_Polygon', 'ST_MultiPolygon')),
            inputs      jsonb       NOT NULL
                        CHECK (jsonb_typeof(inputs) = 'object'
                               AND inputs <> '{}'::jsonb),
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (scenario_id, ordinal)
        )
    """)
    op.execute(
        "CREATE INDEX scenario_rule_geometry_gist "
        "ON curated_geo.scenario_rule USING gist (geometry)"
    )
    op.execute(CONVERT_EDITS)
    op.execute("DROP VIEW IF EXISTS explore.scenario_v")
    op.execute("DROP TABLE curated_geo.scenario_edit")
    op.execute(_scenario_view("rule_count", "curated_geo.scenario_rule"))

    for table, name in _SCENARIO_FKS:
        op.execute(f"""
            DELETE FROM {table} t
            WHERE t.scenario_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM app_core.scenario s
                               WHERE s.id = t.scenario_id)
        """)
        op.execute(f"""
            ALTER TABLE {table} ADD CONSTRAINT {name}
              FOREIGN KEY (scenario_id) REFERENCES app_core.scenario(id)
              ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
        """)
    for statement in DELETE_ORPHAN_PARTITIONS:
        op.execute(statement)
    # The cascade looks rows up by scenario_id; the other three tables already
    # have an index leading with it.
    op.execute(
        "CREATE INDEX aoi_indicator_summary_scenario_idx "
        "ON curated_geo.aoi_indicator_summary (scenario_id) "
        "WHERE scenario_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS curated_geo.aoi_indicator_summary_scenario_idx")
    for table, name in _SCENARIO_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(_SCENARIO_EDIT_DDL)
    op.execute(
        "CREATE INDEX scenario_edit_scenario_idx "
        "ON curated_geo.scenario_edit (scenario_id)"
    )
    op.execute(
        "CREATE INDEX scenario_edit_geometry_gist "
        "ON curated_geo.scenario_edit USING gist (geometry)"
    )
    op.execute(EXPAND_RULES)
    op.execute("DROP VIEW IF EXISTS explore.scenario_v")
    op.execute("DROP TABLE curated_geo.scenario_rule")
    op.execute(_scenario_view("edit_count", "curated_geo.scenario_edit"))
