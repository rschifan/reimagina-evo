"""Scenario authoring — the named scenario, and the areas drawn over it.

r030 deferred ``app_core.scenario`` ("the entity is deferred") and left
``curated_geo.scenario_override.scenario_id`` a bare uuid. This migration adds
the entity and the authoring input that sits upstream of overrides:

``app_core.scenario``
    A named what-if inside a project. ``project_id`` is required and
    ``aoi_id`` is not: a scenario may be created before any AOI exists, so
    the project cannot be derived from the AOI and has to be stated.
    ``UNIQUE (project_id, name)`` is what lets a scenario be addressed by name.
    ``materialized_at`` is nullable and written by a later step, when the
    drawn edits have been expanded into per-feature overrides; null means "not
    yet", which is a different statement from "materialized with nothing to
    do".

``curated_geo.scenario_edit``
    One drawn area carrying one attribute value. ``ordinal`` decides "later
    edits win", not ``created_at``: ``now()`` is transaction-start time, so two
    edits written together are indistinguishable by timestamp. The geometry
    CHECK is a type check only (polygon or multipolygon); validity is the
    API's job on write. ``type_id`` is an FK to the feature-type catalog so a
    typo in a type is impossible. There is deliberately no ``value_text``:
    every authorable attribute is numeric.

Row-level security is enabled on ``app_core.scenario`` only, with the same
permissive-when-unset predicate r011 uses, keyed on ``project_id``.
``scenario_edit`` carries no ``project_id`` and no policy; RLS does not
propagate across the FK, so its rows are tenant-visible even where the
scenario row is not. That is declared in the design rather than papered over.

``explore.scenario_v`` is the engineer-facing listing (AGENTS.md keeps CLI
inspection commands reading from ``explore``): one row per scenario with its
edit and override counts. The counts are correlated subqueries rather than
joins so each stays one row per scenario without a GROUP BY over two
independently fanning-out children. ``override_count`` reads
``scenario_override`` by its bare ``scenario_id``, the convention r030 set.

The downgrade drops the view, then ``scenario_edit``, then ``scenario``; the
policy goes with its table.

Revision ID: r048
Revises: r047
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "r048"
down_revision = "r047"
branch_labels = None
depends_on = None

# The predicate r011 attaches to curated_geo.area_of_interest: unrestricted
# while app.current_project_id is unset, otherwise only that project's rows.
_PERMISSIVE_CONDITION = """
    current_setting('app.current_project_id', true) IS NULL
    OR current_setting('app.current_project_id', true) = ''
    OR {col}::text = current_setting('app.current_project_id', true)
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app_core.scenario (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id      uuid        NOT NULL
                                        REFERENCES app_core.project(id) ON DELETE CASCADE,
            aoi_id          uuid        REFERENCES curated_geo.area_of_interest(id)
                                        ON DELETE SET NULL,
            name            text        NOT NULL,
            description     text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            materialized_at timestamptz,
            UNIQUE (project_id, name)
        )
    """)
    op.execute("ALTER TABLE app_core.scenario ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON app_core.scenario
            USING ({_PERMISSIVE_CONDITION.format(col="project_id")})
    """)

    op.execute("""
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
    """)
    op.execute(
        "CREATE INDEX scenario_edit_scenario_idx "
        "ON curated_geo.scenario_edit (scenario_id)"
    )
    op.execute(
        "CREATE INDEX scenario_edit_geometry_gist "
        "ON curated_geo.scenario_edit USING gist (geometry)"
    )

    op.execute("""
        CREATE OR REPLACE VIEW explore.scenario_v AS
        SELECT
            s.id,
            s.project_id,
            s.name,
            s.description,
            s.aoi_id,
            (SELECT count(*) FROM curated_geo.scenario_edit e
              WHERE e.scenario_id = s.id)     AS edit_count,
            (SELECT count(*) FROM curated_geo.scenario_override o
              WHERE o.scenario_id = s.id)     AS override_count,
            s.materialized_at,
            s.created_at,
            s.updated_at
        FROM app_core.scenario s
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS explore.scenario_v")
    op.execute("DROP TABLE IF EXISTS curated_geo.scenario_edit")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON app_core.scenario")
    op.execute("DROP TABLE IF EXISTS app_core.scenario")
