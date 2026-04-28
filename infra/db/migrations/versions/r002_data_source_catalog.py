"""data_source_catalog and ingestion_run tables.

Revision ID: r002
Revises: r001
Create Date: 2026-04-12
"""

from __future__ import annotations
from alembic import op

revision = "r002"
down_revision = "r001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.data_source_catalog (
            id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            name                  text        NOT NULL,
            provider              text        NOT NULL,
            storage_type          text        NOT NULL
                                              CHECK (storage_type IN
                                                ('vector','tabular','raster','imagery','point_cloud')),
            integration_role      text        NOT NULL
                                              CHECK (integration_role IN
                                                ('canonical_entity','enrichment','model_output')),
            storage_uri           text        NOT NULL,
            schema_definition     jsonb,
            version               text        NOT NULL,
            license               text,
            update_frequency      text
                                              CHECK (update_frequency IS NULL OR update_frequency IN
                                                ('daily','weekly','monthly','quarterly','annual','on_demand')),
            source_extent_geometry geometry(Polygon, 4326),
            ingested_at           timestamptz,
            notes                 text,
            created_at            timestamptz NOT NULL DEFAULT now(),
            UNIQUE (provider, name, version)
        )
    """)

    op.execute("""
        CREATE TABLE curated_geo.ingestion_run (
            id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id           uuid        NOT NULL
                                            REFERENCES curated_geo.data_source_catalog(id),
            orchestrator        text        NOT NULL,
            orchestrator_run_id text        NOT NULL,
            transformation_step text        NOT NULL,
            code_version        text        NOT NULL,
            status              text        NOT NULL
                                            CHECK (status IN ('running','succeeded','failed','cancelled')),
            started_at          timestamptz NOT NULL DEFAULT now(),
            completed_at        timestamptz,
            details             jsonb       NOT NULL DEFAULT '{}',
            CHECK (completed_at IS NULL OR completed_at >= started_at),
            UNIQUE (source_id, orchestrator, orchestrator_run_id, transformation_step)
        )
    """)

    op.execute("""
        CREATE INDEX ingestion_run_source_idx ON curated_geo.ingestion_run (source_id)
    """)
    op.execute("""
        CREATE INDEX ingestion_run_status_idx ON curated_geo.ingestion_run (status)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.ingestion_run CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.data_source_catalog CASCADE")
