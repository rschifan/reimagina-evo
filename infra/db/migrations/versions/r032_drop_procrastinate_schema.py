"""Drop the Procrastinate ops-plane schema (Track A: DBOS).

This branch replaces the queue-based ops-plane with durable execution as a
library. DBOS keeps its checkpoints in the ``dbos`` schema of this same
database, so the ``procrastinate`` schema — its jobs table, its locks, its
triggers and its stored functions — has no reader left.

⚠ ``r029`` is deliberately **not** edited to stop creating it. That revision is
mixed: it adds ``jobs_runtime.orchestration_run.aoi_id``, which this branch
keeps, *and* installs the Procrastinate schema, which it does not. Rewriting an
applied revision would break alembic history for any database already at r030 or
r031, so the schema is created by history and dropped here. A fresh database
therefore passes briefly through a state it never uses, which is the normal cost
of immutable migrations.

What is **not** dropped, and why:

- ``jobs_runtime.orchestration_run`` and ``job_unit`` — this track keeps its own
  run state. That is what preserves the ``partial`` and ``skipped`` vocabulary an
  engine-owned status model would collapse. The service-engine branch drops them.
- ``jobs_runtime.job_run`` — domain provenance, not scheduler state.
  ``curated_geo.indicator_result.job_run_id`` is NOT NULL with an existence
  trigger (r016), and r031 gave it the ``run_id`` and ``backend_pid`` that make a
  run cancellable. It survives on every branch.

Revision ID: r032
Revises: r031
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "r032"
down_revision = "r031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CASCADE because Procrastinate installs functions, triggers and types
    # alongside its tables; dropping the schema is the only tractable way to be
    # sure none is left behind referencing the rest.
    op.execute("DROP SCHEMA IF EXISTS procrastinate CASCADE")


def downgrade() -> None:
    """Reinstall the schema, if the library is still available to describe it.

    Downgrading past this point only makes sense while rolling back to the
    queue-based ops-plane, which needs the library present anyway. Without it
    there is nothing to reinstall and nothing that would read the result, so the
    downgrade succeeds and leaves the schema absent rather than failing.
    """
    try:
        from procrastinate.schema import SchemaManager
    except ImportError:
        return

    op.execute("CREATE SCHEMA IF NOT EXISTS procrastinate")
    op.execute("SET LOCAL search_path = procrastinate, public")
    op.execute(SchemaManager.get_schema())
    op.execute("SET LOCAL search_path = public")
