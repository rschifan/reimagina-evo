"""The resolved plan, stored once and referenced by handle.

⚠ **Revision ranges are split across the fork.** Both tracks branch from r031
and Alembic identifies a revision by its id alone, so a number minted on one
branch and reused on the other yields two migrations answering to one name —
each branch resolving the other's id to its own file. That happened with r033,
and it is how a Hatchet migration silently applied to a database on *this*
lineage, dropping ``orchestration_run`` and ``job_unit`` while ``alembic
current`` still reported "head". **This track keeps r03x/r04x; the Hatchet track
takes r050+.** Never continue the other branch's numbering.

Both ops planes currently ship the dirty feature set *inside* the workflow
input: one uuid per feature per dirty indicator. On a full CDMX recompute that
is 86,225 blocks × 3 dirty indicators = 258,675 uuids, ~10 MB. The Hatchet track
cannot submit it at all — gRPC refuses at 4 MiB — and this track only escapes
because its input goes to Postgres rather than across a wire.

This table is the claim check. The submitter resolves the plan, writes it here
under a ``plan_id`` it mints, and sends the engine a handle. The payload stops
scaling with the city.

⚠ ``feature_ids uuid[]``, one row per unit — not one row per feature. A
row-per-feature table would be 258,675 rows and tens of MB of heap and index for
a single run, and would need ``COPY`` to write in reasonable time. The array form
is 3 rows, written with one ``execute_values``, and Postgres TOASTs and
compresses the 1.4 MB payload out of line. uuid is 16 bytes in an array, not the
36 it costs as text on the wire.

⚠ Written once, never updated. That immutability is the point: replay, retry and
crash recovery all read the identical set. Re-deriving the features at execution
time — from an AOI or a bbox — would be smaller still and is the wrong answer;
spike 2 (``PHASE3_OPS_PLANE_DBOS_SPIKES.md``) found that work re-derived after
an interruption silently attributes one indicator's result to another.

⚠ No index beyond the primary key, deliberately. Rows are deleted when their run
reaches a terminal state, so this table is empty in the steady state and holds
only orphans — a submit that crashed, or a plan abandoned by an idempotency
collision. The TTL sweep that reclaims those scans a table with tens of rows.
Add an index on ``created_at`` if that stops being true.

``now()`` rather than ``clock_timestamp()``: every row of one plan is written in
one transaction and shares a creation time by definition. The distinction that
mattered for ``job_run`` durations (r0xx) does not apply to a creation stamp.

Revision ID: r036
Revises: r035
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "r036"
down_revision = "r035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs_runtime.run_plan (
            plan_id        uuid        NOT NULL,
            indicator_slug text        NOT NULL,
            feature_ids    uuid[]      NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (plan_id, indicator_slug)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobs_runtime.run_plan")
