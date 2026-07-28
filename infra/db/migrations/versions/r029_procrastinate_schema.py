"""Procrastinate task-queue schema (async ops-plane, components 6/7/8) +
orchestration_run.aoi_id.

Procrastinate's own tables/functions/types are installed into a dedicated
``procrastinate`` schema so the whole set drops cleanly on downgrade (the
worker/enqueue connections set ``search_path=procrastinate,public``). Adds
``orchestration_run.aoi_id`` so the async distributor can pass run scope to
``compute_unit``.

Revision ID: r029
Revises: r028
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "r029"
down_revision = "r028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs_runtime.orchestration_run ADD COLUMN aoi_id text")

    # ⚠ Conditional because this branch has dropped the dependency, and a
    # revision cannot import a library the project no longer installs. The DDL
    # itself is untouched: rewriting what an applied revision does to a database
    # breaks alembic history for anything already at r030 or later.
    #
    # When the library is absent the install is skipped; when it is present the
    # schema is created and then dropped again by r033. Both paths end
    # identically, which is what makes the edit safe.
    try:
        from procrastinate.schema import SchemaManager
    except ImportError:
        return

    op.execute("CREATE SCHEMA IF NOT EXISTS procrastinate")
    # Install Procrastinate's objects into the dedicated schema. SET LOCAL is
    # scoped to this migration's transaction, so unqualified CREATEs land in
    # `procrastinate`; runtime resolution uses the connection search_path.
    op.execute("SET LOCAL search_path = procrastinate, public")
    op.execute(SchemaManager.get_schema())
    op.execute("SET LOCAL search_path = public")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS procrastinate CASCADE")
    op.execute(
        "ALTER TABLE jobs_runtime.orchestration_run DROP COLUMN IF EXISTS aoi_id"
    )
