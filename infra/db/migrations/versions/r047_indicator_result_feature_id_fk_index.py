"""Give ``indicator_result.feature_id``'s foreign key an index it can use.

Every index on that column is *partial*. ``ir_one_current_base``,
``ir_one_current_scenario`` and ``ir_feature_scenario_idx`` all carry
``WHERE status = 'current'`` (r013, r025), which is right for the read paths
that motivated them and useless for the one thing a foreign key does: prove that
*no* row references the key being deleted, ``stale`` rows included. Postgres
therefore cannot use any of them to enforce
``indicator_result_feature_id_fkey`` and falls back to a sequential scan of the
whole table — once per deleted feature.

Measured on the demo database (5,249,076 rows, 437,629 features), deleting 25
features::

    Trigger for constraint feature_parent_id_fkey:            0.9 ms
    Trigger for constraint observation_feature_id_fkey:       1.5 ms
    Trigger for constraint aoi_feature_feature_id_fkey:       0.5 ms
    Trigger for constraint feature_overlap_*_fkey:            1.7 ms
    Trigger for constraint indicator_result_feature_id_fkey: 7306.1 ms   <-
    Execution Time:                                          7312.3 ms

292 ms per feature, against sub-millisecond for every other referencing table.
Removing one grain — 2,431 features — took **twelve minutes**, essentially all of
it in that one trigger. With this index the same delete runs in **82 ms**.

That cost is not specific to any one cleanup. It is paid by every path that
removes a feature: retiring a grain, re-ingesting an AOI, or a cascade from
``area_of_interest``. It simply never showed up before, because nothing had
deleted features at volume.

⚠ This is a *plain, non-partial* index, and the redundancy with the partial ones
is the point rather than an oversight. A narrower index cannot serve the
constraint, which is how the table ended up with four indexes on this column and
no usable one.

Plain ``CREATE INDEX``, not ``CONCURRENTLY``, so it runs inside Alembic's
migration transaction — the same choice r026 and r046 make, and here the
measurement supports it directly: the build takes 1.5 s and 39 MB on the
5.2 M-row demo database, so the write-block is not worth trading for the
autocommit block ``CONCURRENTLY`` would require.

Revision ID: r047
Revises: r046
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "r047"
down_revision = "r046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS indicator_result_feature_id_idx "
        "ON curated_geo.indicator_result (feature_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS curated_geo.indicator_result_feature_id_idx")
