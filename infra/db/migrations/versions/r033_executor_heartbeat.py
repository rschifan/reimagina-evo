"""Executor liveness for DBOS operations without an external control plane.

This is deliberately observation, not cross-executor recovery. A heartbeat can
establish that an executor is probably stale; it cannot prove that no live peer
is still executing work. DBOS 2.28 recovery is not idempotent across executors,
so using this table to reclaim work would permit double compute.

Revision ID: r033
Revises: r032
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "r033"
down_revision = "r032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs_runtime.ops_executor_heartbeat (
            executor_id  text PRIMARY KEY,
            role         text NOT NULL,
            app_version  text,
            started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
            heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (length(executor_id) > 0),
            CHECK (length(role) > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ops_executor_heartbeat_at_idx "
        "ON jobs_runtime.ops_executor_heartbeat (heartbeat_at DESC)"
    )
    op.execute(
        """
        CREATE VIEW explore.ops_executor_health_v AS
        SELECT executor_id,
               role,
               app_version,
               started_at,
               heartbeat_at,
               clock_timestamp() - heartbeat_at AS heartbeat_age
        FROM jobs_runtime.ops_executor_heartbeat
        ORDER BY heartbeat_at DESC, executor_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS explore.ops_executor_health_v")
    op.execute("DROP TABLE IF EXISTS jobs_runtime.ops_executor_heartbeat")
