"""Index ``orchestration_run.submitted_at`` for the Ops dashboard's run list.

The dashboard's ``GET /jobs`` orders by ``submitted_at DESC LIMIT 50`` and is
polled every two seconds per open tab. r028 created only a primary key and
``orchestration_run_status_idx (status)``, neither of which can serve that
ordering, so every tick sorted the whole table.

Revision ID: r046
Revises: r045
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "r046"
down_revision = "r045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain CREATE INDEX (not CONCURRENTLY) so the operation runs inside
    # Alembic's migration transaction, exactly as r026 does and for the same
    # reason: this table holds one row per submitted run, so it is far smaller
    # than the observation table that precedent was set on, and the write-block
    # is not measurable.
    op.execute(
        "CREATE INDEX IF NOT EXISTS orchestration_run_submitted_at_idx "
        "ON jobs_runtime.orchestration_run (submitted_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_runtime.orchestration_run_submitted_at_idx")
