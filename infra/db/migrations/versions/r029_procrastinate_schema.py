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

    # ⚠ Edited on the DBOS branch, and only the import is conditional.
    #
    # This revision is mixed: the aoi_id column above is kept by every branch,
    # while the schema below belongs to an ops-plane this branch replaced. A
    # revision cannot import a dependency the project no longer has, and keeping
    # `procrastinate` installed purely so a historical migration can run would
    # misreport this branch's dependency footprint — one of the things the two
    # ops-planes are being compared on.
    #
    # Skipping the install is safe because r032 drops this schema immediately
    # afterwards: a database that never creates it and one that creates then
    # drops it reach the same state. Rewriting history to remove the install
    # instead would break alembic for any database already at r030 or later,
    # which is why that is not done.
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
