"""Create schemas, extensions, and app_core.project stub.

Revision ID: r001
Revises:
Create Date: 2026-04-12
"""

from __future__ import annotations
from alembic import op

revision = "r001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE SCHEMA IF NOT EXISTS app_core")
    op.execute("CREATE SCHEMA IF NOT EXISTS jobs_runtime")
    op.execute("CREATE SCHEMA IF NOT EXISTS curated_geo")
    op.execute("CREATE SCHEMA IF NOT EXISTS serving_geo")

    # Minimal project stub — needed by area_of_interest FK.
    # Fleshed out fully in Layer 3 (scenarios).
    op.execute("""
        CREATE TABLE IF NOT EXISTS app_core.project (
            id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text        NOT NULL,
            description text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_core.project CASCADE")
    op.execute("DROP SCHEMA IF EXISTS serving_geo CASCADE")
    op.execute("DROP SCHEMA IF EXISTS curated_geo CASCADE")
    op.execute("DROP SCHEMA IF EXISTS jobs_runtime CASCADE")
    op.execute("DROP SCHEMA IF EXISTS app_core CASCADE")
