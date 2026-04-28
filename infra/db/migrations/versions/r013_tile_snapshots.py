"""Tile snapshot registry for hybrid live-MVT and PMTiles serving.

Revision ID: r013
Revises: r012
Create Date: 2026-04-15
"""

from __future__ import annotations

from alembic import op

revision = "r013"
down_revision = "r012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS curated_geo.tile_snapshot_registry (
            id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            layer_id           text        NOT NULL,
            artifact_path      text        NOT NULL,
            artifact_uri       text,
            snapshot_version   text        NOT NULL,
            source_relation    text        NOT NULL,
            source_lineage_ref jsonb       NOT NULL DEFAULT '{}'::jsonb,
            status             text        NOT NULL DEFAULT 'ready',
            created_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (layer_id, snapshot_version),
            CONSTRAINT tile_snapshot_status_check
                CHECK (status IN ('ready', 'failed', 'superseded'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS tile_snapshot_layer_created_idx
        ON curated_geo.tile_snapshot_registry (layer_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.tile_snapshot_registry CASCADE")
