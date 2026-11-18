"""Index the successful units by indicator, for the run page's duration estimate.

``GET /jobs/{id}/detail`` gives each unit the duration of the last successful
run of the same indicator (``OpsRunReader.expected_durations``), and the page
polls it every second while a run is live. Without this the lookup reads every
``job_unit`` row ever written; partial on ``succeeded`` because no other status
is ever looked up.

Revision ID: r050
Revises: r049
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "r050"
down_revision = "r049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX job_unit_success_by_slug_idx "
        "ON jobs_runtime.job_unit (indicator_slug, completed_at DESC) "
        "WHERE status = 'succeeded'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_runtime.job_unit_success_by_slug_idx")
