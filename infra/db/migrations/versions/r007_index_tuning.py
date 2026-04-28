"""Index tuning for lineage, foreign keys, and inspection access paths.

Revision ID: r007
Revises: r006
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op

revision = "r007"
down_revision = "r006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ingestion_run_source_started_idx
        ON curated_geo.ingestion_run (source_id, started_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS feature_type_id_idx
        ON curated_geo.feature (type_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS feature_ingestion_run_idx
        ON curated_geo.feature (ingestion_run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS observation_ingestion_run_idx
        ON curated_geo.observation (ingestion_run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS file_asset_ingestion_run_idx
        ON curated_geo.file_asset (ingestion_run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS area_of_interest_project_idx
        ON curated_geo.area_of_interest (project_id)
        """
    )


def downgrade() -> None:
    for index in [
        "area_of_interest_project_idx",
        "file_asset_ingestion_run_idx",
        "observation_ingestion_run_idx",
        "feature_ingestion_run_idx",
        "feature_type_id_idx",
        "ingestion_run_source_started_idx",
    ]:
        op.execute(f"DROP INDEX IF EXISTS curated_geo.{index}")
