"""Transactional outbox for scenario edits consumed by the Hatchet event bridge.

Revision ID: r034
Revises: r033
"""

from __future__ import annotations

from alembic import op

revision = "r034"
down_revision = "r033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs_runtime.orchestration_outbox (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id        uuid NOT NULL,
            changed_attributes text[] NOT NULL,
            edited_features    uuid[] NOT NULL,
            created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
            claimed_at         timestamptz,
            claim_token        uuid,
            delivered_at       timestamptz,
            attempts           integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            error_message      text
        )
    """)
    op.execute("""
        CREATE INDEX orchestration_outbox_pending_idx
          ON jobs_runtime.orchestration_outbox (created_at)
          WHERE delivered_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobs_runtime.orchestration_outbox")
