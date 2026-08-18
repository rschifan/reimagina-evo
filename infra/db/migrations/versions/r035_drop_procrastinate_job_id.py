"""Drop the last Procrastinate remnant: ``job_run.procrastinate_job_id``.

r028 added it to link a provenance row to the queue job that ran it. Nothing
ever wrote it — the async distributor it was waiting for never landed on this
lineage, and r032 removed the queue schema entirely. A nullable bigint that no
code reads or writes is not free: it is a column every reader of ``job_run`` has
to decide to ignore, and a standing invitation to reintroduce the coupling.

The Hatchet track dropped it in its own ``r033_drop_our_run_state``. This is the
same removal on this lineage, so neither track carries it and the two agree on
the shape of the one table both keep.

⚠ ``job_run`` itself stays, on every branch. It is domain provenance —
``curated_geo.indicator_result.job_run_id`` is NOT NULL behind an existence
trigger (r016) — and it is where ``backend_pid`` lives, which is the only way to
cancel a unit blocked in a psycopg call on either ops plane.

Revision ID: r035
Revises: r034
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "r035"
down_revision = "r034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE jobs_runtime.job_run DROP COLUMN IF EXISTS procrastinate_job_id"
    )


def downgrade() -> None:
    """Recreate the column, empty.

    ``IF NOT EXISTS`` because this lineage's history contains a period where the
    column was dropped out-of-band by a Hatchet migration misdirected at a
    database on this branch; a downgrade that assumed its absence would fail on
    exactly the databases most likely to need one.
    """
    op.execute(
        "ALTER TABLE jobs_runtime.job_run "
        "ADD COLUMN IF NOT EXISTS procrastinate_job_id bigint"
    )
