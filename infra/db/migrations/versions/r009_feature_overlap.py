"""feature_overlap and feature_overlap_type_pair tables.

Pre-computes spatial intersections between arbitrary pairs of feature types,
generalising aoi_feature to feature-to-feature.  This unblocks cross-grain
indicator aggregation (TODO #1 / #3) and lets the rollup worker operate on
non-hierarchical spatial relationships without requiring feature.parent_id.

Revision ID: r009
Revises: r008
Create Date: 2026-04-14
"""
from __future__ import annotations

from alembic import op

revision = "r009"
down_revision = "r008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Declares which (source_type, target_type) pairs should have overlaps computed
    op.execute("""
        CREATE TABLE curated_geo.feature_overlap_type_pair (
            id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
            source_type_id UUID    NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
            target_type_id UUID    NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
            UNIQUE (source_type_id, target_type_id)
        )
    """)

    # Pre-computed spatial intersections — generalises aoi_feature to feature×feature
    op.execute("""
        CREATE TABLE curated_geo.feature_overlap (
            id                UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
            source_feature_id UUID             NOT NULL
                                               REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
            target_feature_id UUID             NOT NULL
                                               REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
            overlap_basis     TEXT             NOT NULL
                                               CHECK (overlap_basis IN ('area', 'length', 'presence')),
            overlap_measure   DOUBLE PRECISION NOT NULL CHECK (overlap_measure > 0),
            feature_measure   DOUBLE PRECISION NOT NULL CHECK (feature_measure > 0),
            overlap_ratio     DOUBLE PRECISION NOT NULL CHECK (overlap_ratio > 0 AND overlap_ratio <= 1),
            computed_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
            UNIQUE (source_feature_id, target_feature_id)
        )
    """)

    op.execute("""
        CREATE INDEX feature_overlap_target_idx
        ON curated_geo.feature_overlap (target_feature_id, overlap_ratio)
    """)
    op.execute("""
        CREATE INDEX feature_overlap_source_idx
        ON curated_geo.feature_overlap (source_feature_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.feature_overlap CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.feature_overlap_type_pair CASCADE")
