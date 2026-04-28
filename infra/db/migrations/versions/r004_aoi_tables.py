"""area_of_interest, aoi_feature tables and trigger.

Revision ID: r004
Revises: r003
Create Date: 2026-04-12
"""

from __future__ import annotations
from alembic import op

revision = "r004"
down_revision = "r003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.area_of_interest (
            id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id  uuid        NOT NULL REFERENCES app_core.project(id) ON DELETE CASCADE,
            name        text        NOT NULL,
            description text,
            geometry    geometry(Geometry, 4326) NOT NULL
                        CHECK (ST_GeometryType(geometry) IN ('ST_Polygon','ST_MultiPolygon')),
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX aoi_geometry_gist ON curated_geo.area_of_interest USING GIST (geometry)
    """)

    op.execute("""
        CREATE TABLE curated_geo.aoi_feature (
            aoi_id          uuid             NOT NULL
                                             REFERENCES curated_geo.area_of_interest(id) ON DELETE CASCADE,
            feature_id      uuid             NOT NULL
                                             REFERENCES curated_geo.feature(id)           ON DELETE CASCADE,
            overlap_basis   text             NOT NULL
                                             CHECK (overlap_basis IN ('area','length','presence')),
            overlap_measure double precision NOT NULL CHECK (overlap_measure > 0),
            feature_measure double precision NOT NULL CHECK (feature_measure > 0),
            overlap_ratio   double precision NOT NULL CHECK (overlap_ratio > 0 AND overlap_ratio <= 1),
            PRIMARY KEY (aoi_id, feature_id),
            CHECK (overlap_measure <= feature_measure),
            CHECK (
                overlap_basis <> 'presence'
                OR (overlap_measure = 1 AND feature_measure = 1 AND overlap_ratio = 1)
            )
        )
    """)

    op.execute("""
        CREATE INDEX aoi_feature_feature_idx ON curated_geo.aoi_feature (feature_id)
    """)

    # Trigger: validate overlap_basis matches the feature's geometry family
    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.aoi_feature_before_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_geom_type text;
        BEGIN
            SELECT ST_GeometryType(geometry) INTO v_geom_type
              FROM curated_geo.feature WHERE id = NEW.feature_id;

            IF NEW.overlap_basis = 'presence'
               AND v_geom_type NOT IN ('ST_Point','ST_MultiPoint') THEN
                RAISE EXCEPTION 'overlap_basis=presence requires a point feature, got %', v_geom_type;
            END IF;
            IF NEW.overlap_basis = 'length'
               AND v_geom_type NOT IN ('ST_LineString','ST_MultiLineString') THEN
                RAISE EXCEPTION 'overlap_basis=length requires a line feature, got %', v_geom_type;
            END IF;
            IF NEW.overlap_basis = 'area'
               AND v_geom_type NOT IN ('ST_Polygon','ST_MultiPolygon') THEN
                RAISE EXCEPTION 'overlap_basis=area requires a polygon feature, got %', v_geom_type;
            END IF;

            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER aoi_feature_before_write
            BEFORE INSERT ON curated_geo.aoi_feature
            FOR EACH ROW EXECUTE FUNCTION curated_geo.aoi_feature_before_write()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.aoi_feature CASCADE")
    op.execute("DROP FUNCTION IF EXISTS curated_geo.aoi_feature_before_write CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.area_of_interest CASCADE")
