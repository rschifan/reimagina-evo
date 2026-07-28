"""Run correlation + backend pid on job_run — the cancellation seam.

Cancelling a run has to reach the Postgres backends actually doing the work,
because no orchestration engine can interrupt a thread blocked in a psycopg C
call: cancellation is only observed at the next step boundary, which for a
minutes-long PostGIS statement is far too late. The one mechanism that does
work is ``pg_cancel_backend`` — and to use it you need two facts this table
never recorded.

``backend_pid``
    The Postgres backend serving the connection running the compute, captured
    with ``pg_backend_pid()`` at insert. Whoever wants to stop the query needs
    the pid, and only the working connection itself can supply it.

``run_id``
    Which orchestration run a job_run belongs to. ``job_run`` has no link to a
    run today — the only unit→job_run edge is ``job_unit.job_run_id`` (r028),
    which is nullable and has never been written — so "cancel this run" cannot
    find its backends. Nullable and with no FK, deliberately: a direct CLI
    compute belongs to no run, and an ops-plane that owns run state natively
    drops ``jobs_runtime.orchestration_run`` entirely, which an FK would forbid.

``job_run`` itself survives every ops-plane choice: it is domain provenance
(``curated_geo.indicator_result.job_run_id`` is NOT NULL with an existence
trigger, r016), not scheduler state.

Revision ID: r031
Revises: r030
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "r031"
down_revision = "r030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs_runtime.job_run ADD COLUMN run_id uuid")
    op.execute("ALTER TABLE jobs_runtime.job_run ADD COLUMN backend_pid int")
    # The cancel path's only query: this run's still-live rows. Partial, because
    # rows with no run_id (direct CLI computes) are never looked up this way.
    op.execute(
        "CREATE INDEX job_run_run_idx ON jobs_runtime.job_run (run_id, status) "
        "WHERE run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_runtime.job_run_run_idx")
    op.execute("ALTER TABLE jobs_runtime.job_run DROP COLUMN IF EXISTS backend_pid")
    op.execute("ALTER TABLE jobs_runtime.job_run DROP COLUMN IF EXISTS run_id")
