"""Index `run_plan.created_at` — the steady state r050 assumed is gone.

r050 declined an index and said so explicitly: "Rows are deleted when their run
reaches a terminal state, so this table is empty in the steady state and holds
only orphans… Add an index on ``created_at`` only if that stops being true."

It has stopped being true, deliberately.

Deleting a plan at terminal state destroyed the input of every *replayable* run.
Hatchet retains run history for 720 h by default and `runs.replay()` re-executes
from the original input — which, on this track, is a `plan_id` handle whose rows
the finalizer had already reclaimed. A replay of anything older than the sweep's
24 h horizon failed every unit with a missing-plan error, and the sweep ran on
the *submit* path, so an ordinary busy afternoon could delete a live run's plan.

So the table is now retained, not reclaimed. Plans live at least as long as the
engine keeps the runs that reference them, which makes `created_at` the access
path for retention and makes this index load-bearing rather than speculative.

⚠ **The retention horizon must exceed engine retention.** Not "should" —
`SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD` bounds how long a run can be
replayed, and a plan that expires first turns a replayable run into a run that
fails on contact. Both values are deployment configuration and are pinned to
each other; see `docs/architecture/OPS_PLANE_RUNBOOK.md`.

⚠ Retention is a **Postgres-owned operation outside request submission**. It is
not on the submit path, and it is not a Hatchet cron — putting it in the engine
would make plan retention depend on the engine being healthy, which is exactly
backwards for the table that exists to survive independently. The statement is
in `scripts/reclaim_run_plans.sql`, run by the platform's scheduler.

Two deletes remain in application code, and only because they are *provable*: an
idempotency collision (the accepted run carries a different `plan_id`) and a
trigger that definitively never reached the engine. Anything ambiguous retains.

# ponytail: plain index + scheduled DELETE. Partition by created_at if the scan
# ever shows up in pg_stat_statements — it will not at three rows per run.

Revision ID: r051
Revises: r050
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "r051"
down_revision = "r050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS run_plan_created_idx
        ON jobs_runtime.run_plan (created_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_runtime.run_plan_created_idx")
