"""Orchestration run envelope + per-unit task state (component 5) + job_run link.

- ``jobs_runtime.orchestration_run`` — one row per ``submit_run`` (the run
  envelope the Orchestrator port's ``get_run`` reads).
- ``jobs_runtime.job_unit`` — one row per ``DirtyUnit`` within a run (the
  "one row per unit of work" the ops-plane needs; did not exist before).
- ``job_run`` gains ``procrastinate_job_id`` linking a provenance row to the
  ops job that ran it (NULL in the synchronous path; populated in Phase 4).

Postgres-only — DuckDB is frozen at r024 and runs the synchronous orchestrator
with an in-memory RunReport, so it does not persist orchestration state.

Revision ID: r028
Revises: r027
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op

revision = "r028"
down_revision = "r027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs_runtime.orchestration_run (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id   uuid,
            status        text        NOT NULL
                                      CHECK (status IN ('queued','running','succeeded','partial','failed','cancelling','cancelled')),
            code_version  text        NOT NULL,
            submitted_at  timestamptz NOT NULL DEFAULT now(),
            completed_at  timestamptz,
            CHECK (completed_at IS NULL OR completed_at >= submitted_at)
        )
    """)
    op.execute(
        "CREATE INDEX orchestration_run_status_idx "
        "ON jobs_runtime.orchestration_run (status)"
    )

    op.execute("""
        CREATE TABLE jobs_runtime.job_unit (
            id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id          uuid        NOT NULL
                                        REFERENCES jobs_runtime.orchestration_run(id) ON DELETE CASCADE,
            indicator_slug  text        NOT NULL,
            feature_scope   jsonb       NOT NULL DEFAULT '[]'::jsonb,
            status          text        NOT NULL
                                        CHECK (status IN ('queued','running','succeeded','failed','skipped','cancelled')),
            detail          text,
            job_run_id      uuid        REFERENCES jobs_runtime.job_run(id) ON DELETE SET NULL,
            queued_at       timestamptz NOT NULL DEFAULT now(),
            started_at      timestamptz,
            completed_at    timestamptz,
            UNIQUE (run_id, indicator_slug)
        )
    """)
    op.execute(
        "CREATE INDEX job_unit_run_idx ON jobs_runtime.job_unit (run_id, status)"
    )

    # Link a provenance run to the ops (Procrastinate) job that ran it. NULL
    # in the synchronous path; populated when the async distributor lands.
    op.execute(
        "ALTER TABLE jobs_runtime.job_run ADD COLUMN procrastinate_job_id bigint"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE jobs_runtime.job_run DROP COLUMN IF EXISTS procrastinate_job_id"
    )
    op.execute("DROP TABLE IF EXISTS jobs_runtime.job_unit")
    op.execute("DROP TABLE IF EXISTS jobs_runtime.orchestration_run")
