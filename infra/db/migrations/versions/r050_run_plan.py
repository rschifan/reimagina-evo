"""The resolved plan, stored once and referenced by handle.

⚠ **Numbered r050, and the gap is the point.** Both tracks fork from r031 and
Alembic identifies a revision by its id alone, so a number minted on one branch
and reused on the other produces two migrations answering to one name — each
branch resolving the other's id to its own file. That already happened once with
r033 and it is how a Hatchet migration silently applied to a database on the
DBOS lineage, dropping tables while ``alembic current`` reported "head".

So the ranges are now split, permanently: **this track takes r050+, the DBOS
track keeps r03x/r04x.** Sequential numbering across a fork is a collision
waiting for the next person; pick from this range and it cannot happen.

---

`RunInput.features` ships the dirty feature set *inside* the workflow input: one
uuid per feature per dirty indicator. A full CDMX recompute is 86,225 blocks × 3
dirty indicators = 258,675 uuids, and measured on real data that is 10,088,804
bytes against gRPC's 4,194,304 default. **The run cannot be submitted at all** —
`RESOURCE_EXHAUSTED`, before the engine sees anything. The ceiling is ~35,800
blocks, 42% of the city.

This table is the claim check. The submitter resolves the plan, writes it here
under a ``plan_id`` it mints, and hands the engine a couple of hundred bytes.
The payload stops scaling with the city.

⚠ This branch otherwise owns only ``job_run`` in ``jobs_runtime``; r033 deleted
the rest deliberately, because the engine owns run *state*. A plan is not run
state — it is the decision of what the run contains, which this project's own
position holds is never the engine's. Serialising that decision into the
engine's message was the anomaly; persisting it here is the boundary working.

⚠ ``feature_ids uuid[]``, one row per unit — not one row per feature. A
row-per-feature table would be 258,675 rows and tens of MB of heap and index per
run, needing ``COPY``. The array form is 3 rows via one ``execute_values``, and
Postgres TOASTs and compresses the 1.4 MB out of line. uuid is 16 bytes in an
array against the 36 it costs as text on the wire.

⚠ Written once, never updated. Replay, retry and recovery therefore read the
identical set. Re-deriving features at execution time from an AOI or bbox would
be smaller still and is the wrong answer: spike 2 found that re-derived work
silently attributes one indicator's result to another.

⚠ No index beyond the primary key, deliberately. Rows are deleted when their run
reaches a terminal state, so this table is empty in the steady state and holds
only orphans — a crashed submit, or a plan abandoned when the idempotency window
returned an existing run. The TTL sweep that reclaims those scans tens of rows.
Add an index on ``created_at`` only if that stops being true.

``now()`` rather than ``clock_timestamp()``: every row of one plan is written in
one transaction and shares a creation time by definition. The distinction that
mattered for ``job_run`` durations does not apply to a creation stamp.

Revision ID: r050
Revises: r033
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "r050"
down_revision = "r033"
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
