"""Row-Level Security policies for multi-project isolation.

Enables RLS on the two existing project-scoped tables:
  - app_core.project          (tenant root — policy on id)
  - curated_geo.area_of_interest (policy on project_id)

Policy design:
  - Permissive when app.current_project_id is not set (NULL or '').
    Existing application and migration code continues to work unchanged.
  - When set via SET LOCAL, only rows for that project are visible.
  - Table owners bypass RLS in PostgreSQL by default. FORCE RLS and a
    dedicated app role are deferred until the first API route is deployed
    (see docs/ENGINEERING_NOTES.md — Row-Level Security section).

Revision ID: r011
Revises: r010
Create Date: 2026-04-14
"""

from __future__ import annotations
from alembic import op

revision = "r011"
down_revision = "r010"
branch_labels = None
depends_on = None

_PERMISSIVE_CONDITION = """
    current_setting('app.current_project_id', true) IS NULL
    OR current_setting('app.current_project_id', true) = ''
    OR {col}::text = current_setting('app.current_project_id', true)
"""


def upgrade() -> None:
    # ── app_core.project ────────────────────────────────────────────────────
    # The project row IS the tenant root; policy filters on id.
    op.execute("ALTER TABLE app_core.project ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON app_core.project
            USING ({_PERMISSIVE_CONDITION.format(col='id')})
    """)

    # ── curated_geo.area_of_interest ─────────────────────────────────────────
    op.execute("ALTER TABLE curated_geo.area_of_interest ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON curated_geo.area_of_interest
            USING ({_PERMISSIVE_CONDITION.format(col='project_id')})
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON curated_geo.area_of_interest")
    op.execute("ALTER TABLE curated_geo.area_of_interest DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON app_core.project")
    op.execute("ALTER TABLE app_core.project DISABLE ROW LEVEL SECURITY")
