"""Add a covering observation index keyed on (attribute_key, feature_id).

The tile-source resolver enumerates (attribute_key, type_slug, aoi_id)
tuples for every current observation to compute per-overlay AOI
affinity. The plan was a parallel sequential scan over
``curated_geo.observation`` even with the existing partial index
``obs_one_current`` because that index leads with ``feature_id`` —
fine for "find this feature's value", wrong column order for "list
all is_current rows" by attribute_key.

Adding a partial index leading with ``attribute_key`` flips the plan
to a parallel index-only scan, dropping the
:func:`reimagina.tiles.service._list_observation_aoi_affinity`
query from ~660 ms to ~330 ms on the live CDMX + Mexicali dataset
(measured ~221 k current observations across 16 attribute_keys).

Revision ID: r026
Revises: r025
Create Date: 2026-04-28
"""

from __future__ import annotations

from alembic import op


revision = "r026"
down_revision = "r025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain CREATE INDEX (not CONCURRENTLY) so the operation runs
    # inside Alembic's migration transaction. The table is small at
    # the prototype's scale (~1 M rows, ~50 MB index footprint); the
    # write-block during migration is sub-second on the dev DB.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS obs_attr_feat_current
            ON curated_geo.observation (attribute_key, feature_id)
            WHERE is_current
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS curated_geo.obs_attr_feat_current")
