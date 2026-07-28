"""Drop our run-state tables and the Procrastinate schema (Track B: Hatchet).

This is the migration the track exists to make. The engine owns run state end to
end, so the tables that held our version of it have no reader left:

- ``jobs_runtime.orchestration_run`` — the run envelope.
- ``jobs_runtime.job_unit`` — per-unit status.
- ``jobs_runtime.job_run.procrastinate_job_id`` — added by r028, never written.
- the ``procrastinate`` schema — its jobs table, locks, triggers and functions.

⚠ **This is where the two branches become permanently non-mergeable.** Track A
takes ``r032`` off the same parent (r031) and keeps both tables — that is what
preserves the ``partial``/``skipped`` vocabulary in a form you can ``GROUP BY``.
Two children of one revision, deliberately, and neither is a fast-forward of
the other.

**What is lost, precisely.** Not the vocabulary — spike B8 showed the workflow's
``on_failure_task`` reconstructs ``partial`` and ``skipped`` into the run's
output, and ``get_run`` reads it from there. What is lost is *querying across
runs*: aggregates, joins and indexes over run history. ``additional_metadata``
(scenario, AOI, code version) narrows it to a lookup key, so "every run for this
scenario" still works, in ``runs.list()`` and the dashboard's search box. "How
many scenarios partially failed this week" does not.

⚠ **Run history now expires.** The engine's
``SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD`` defaults to 720h, so runs
older than 30 days are deleted. On the other branch history lives in
``orchestration_run`` and is bounded only by disk.

What is **not** dropped, and why:

- ``jobs_runtime.job_run`` — domain provenance, not scheduler state.
  ``curated_geo.indicator_result.job_run_id`` is NOT NULL with an existence
  trigger (r016), and r031 gave it the ``run_id`` and ``backend_pid`` that make
  a run cancellable. Cancellation is cooperative on both engines and neither
  interrupts a blocking psycopg call, so ``pg_cancel_backend`` against that
  column is the only mechanism that actually stops a statement. It survives on
  every branch.

Dropping the two tables is FK-safe: ``bootstrap.py``'s ``REQUIRED_RELATIONS``
does not list them, and the only inbound FK is ``job_unit``'s own reference to
``orchestration_run``.

Revision ID: r033
Revises: r031
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op

revision = "r033"
down_revision = "r031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # job_unit first: it holds the FK to orchestration_run.
    op.execute("DROP TABLE IF EXISTS jobs_runtime.job_unit")
    op.execute("DROP TABLE IF EXISTS jobs_runtime.orchestration_run")
    op.execute(
        "ALTER TABLE jobs_runtime.job_run DROP COLUMN IF EXISTS procrastinate_job_id"
    )
    # CASCADE because Procrastinate installs functions, triggers and types
    # alongside its tables; dropping the schema is the only tractable way to be
    # sure none is left behind referencing the rest.
    op.execute("DROP SCHEMA IF EXISTS procrastinate CASCADE")


def downgrade() -> None:
    """Recreate the structures. The rows are gone and are not recoverable.

    Downgrading past this point only makes sense while rolling back to an
    ops-plane that owns its own run state, which would be repopulating these
    from scratch anyway.

    ⚠ The DDL below is r028's verbatim, plus the ``aoi_id`` column r029 adds.
    It is copied rather than reconstructed on purpose: a first draft written
    from memory got six things wrong — ``scenario_id`` is uuid not text, the
    status CHECK includes ``cancelling``, ``code_version`` has no default,
    ``feature_scope`` is NOT NULL, ``job_run_id`` carries an FK, and the three
    timestamp columns were missing entirely. A downgrade that silently produces
    a differently-shaped table is worse than no downgrade at all.
    """
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
    # Added by r029, which this revision's parent chain still contains.
    op.execute("ALTER TABLE jobs_runtime.orchestration_run ADD COLUMN aoi_id text")

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
    op.execute(
        "ALTER TABLE jobs_runtime.job_run ADD COLUMN procrastinate_job_id bigint"
    )
