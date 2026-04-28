"""feature_type_catalog, feature, observation, file_asset tables and triggers.

Revision ID: r003
Revises: r002
Create Date: 2026-04-12
"""

from __future__ import annotations
from alembic import op

revision = "r003"
down_revision = "r002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── feature_type_catalog ────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.feature_type_catalog (
            id              uuid      PRIMARY KEY DEFAULT gen_random_uuid(),
            context         text      NOT NULL,
            type_slug       text      NOT NULL,
            label           text      NOT NULL,
            geometry_family text      NOT NULL
                                      CHECK (geometry_family IN ('point','line','polygon','geometry')),
            role            text      NOT NULL
                                      CHECK (role IN
                                        ('analysis_unit','context','infrastructure','poi','detection')),
            admin_level     smallint  CHECK (admin_level IS NULL OR admin_level > 0),
            description     text,
            UNIQUE (context, type_slug)
        )
    """)

    # Seed canonical Mexico + global feature types
    op.execute("""
        INSERT INTO curated_geo.feature_type_catalog
            (context, type_slug, label, geometry_family, role, admin_level)
        VALUES
            ('MEX',    'state',         'Estado',        'polygon', 'context',        1),
            ('MEX',    'municipality',  'Municipio',     'polygon', 'context',        2),
            ('MEX',    'neighborhood',  'Colonia',       'polygon', 'context',        3),
            ('MEX',    'ageb',          'AGEB',          'polygon', 'analysis_unit',  4),
            ('MEX',    'block',         'Manzana',       'polygon', 'analysis_unit',  5),
            ('MEX',    'lot',           'Predio',        'polygon', 'analysis_unit',  6),
            ('global', 'building',      'Building',      'polygon', 'context',        NULL),
            ('global', 'tree',          'Tree',          'point',   'detection',      NULL),
            ('global', 'bus_stop',      'Bus Stop',      'point',   'poi',            NULL),
            ('global', 'station',       'Station',       'point',   'poi',            NULL),
            ('global', 'establishment', 'Establishment', 'point',   'poi',            NULL),
            ('global', 'road',          'Road',          'line',    'infrastructure', NULL),
            ('global', 'sidewalk',      'Sidewalk',      'line',    'infrastructure', NULL),
            ('global', 'bike_lane',     'Bike Lane',     'line',    'infrastructure', NULL),
            ('global', 'crossing',      'Crossing',      'point',   'infrastructure', NULL)
        ON CONFLICT (context, type_slug) DO NOTHING
    """)

    # ── feature ─────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.feature (
            id               uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            type_id          uuid             NOT NULL
                                              REFERENCES curated_geo.feature_type_catalog(id),
            type_slug        text             NOT NULL,
            role             text             NOT NULL,
            parent_id        uuid             REFERENCES curated_geo.feature(id),
            name             text,
            geometry         geometry(Geometry, 4326) NOT NULL,
            attributes       jsonb            NOT NULL DEFAULT '{}',
            source_id        uuid             NOT NULL
                                              REFERENCES curated_geo.data_source_catalog(id),
            source_record_ref text            NOT NULL,
            ingestion_run_id uuid             NOT NULL
                                              REFERENCES curated_geo.ingestion_run(id),
            dataset_version  text             NOT NULL,
            confidence       double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            model_version    text,
            created_at       timestamptz      NOT NULL DEFAULT now(),
            updated_at       timestamptz      NOT NULL DEFAULT now(),
            UNIQUE (source_id, dataset_version, source_record_ref)
        )
    """)

    op.execute(
        "CREATE INDEX feature_geometry_gist  ON curated_geo.feature USING GIST (geometry)"
    )
    op.execute("CREATE INDEX feature_type_slug_idx  ON curated_geo.feature (type_slug)")
    op.execute("CREATE INDEX feature_role_idx       ON curated_geo.feature (role)")
    op.execute("CREATE INDEX feature_parent_idx     ON curated_geo.feature (parent_id)")
    op.execute("CREATE INDEX feature_source_idx     ON curated_geo.feature (source_id)")
    op.execute("""
        CREATE INDEX feature_block_geom_idx ON curated_geo.feature USING GIST (geometry)
        WHERE type_slug = 'block'
    """)
    op.execute("""
        CREATE INDEX feature_lot_geom_idx ON curated_geo.feature USING GIST (geometry)
        WHERE type_slug = 'lot'
    """)

    # Trigger: derive type_slug + role from type_id, validate geometry family,
    # validate source_id/ingestion_run alignment, enforce hierarchy rules.
    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.feature_before_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_type_slug        text;
            v_role             text;
            v_geom_family      text;
            v_admin_level      smallint;
            v_run_source_id    uuid;
            v_parent_admin_lvl smallint;
            v_geom_type        text;
            v_cycle_found      boolean;
        BEGIN
            SELECT type_slug, role, geometry_family, admin_level
              INTO v_type_slug, v_role, v_geom_family, v_admin_level
              FROM curated_geo.feature_type_catalog
             WHERE id = NEW.type_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Unknown feature type_id: %', NEW.type_id;
            END IF;

            NEW.type_slug := v_type_slug;
            NEW.role      := v_role;

            -- Validate geometry family
            v_geom_type := ST_GeometryType(NEW.geometry);
            IF v_geom_family = 'point' AND v_geom_type NOT IN ('ST_Point','ST_MultiPoint') THEN
                RAISE EXCEPTION 'Feature type % requires point geometry, got %', v_type_slug, v_geom_type;
            END IF;
            IF v_geom_family = 'line' AND v_geom_type NOT IN ('ST_LineString','ST_MultiLineString') THEN
                RAISE EXCEPTION 'Feature type % requires line geometry, got %', v_type_slug, v_geom_type;
            END IF;
            IF v_geom_family = 'polygon' AND v_geom_type NOT IN ('ST_Polygon','ST_MultiPolygon') THEN
                RAISE EXCEPTION 'Feature type % requires polygon geometry, got %', v_type_slug, v_geom_type;
            END IF;

            -- Validate source_id matches ingestion_run.source_id
            SELECT source_id INTO v_run_source_id
              FROM curated_geo.ingestion_run WHERE id = NEW.ingestion_run_id;
            IF v_run_source_id IS DISTINCT FROM NEW.source_id THEN
                RAISE EXCEPTION 'source_id mismatch: feature.source_id=% but ingestion_run.source_id=%',
                    NEW.source_id, v_run_source_id;
            END IF;

            -- No self-parenting
            IF NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.id THEN
                RAISE EXCEPTION 'Feature cannot be its own parent (id=%)', NEW.id;
            END IF;

            IF NEW.parent_id IS NOT NULL AND v_admin_level IS NULL THEN
                RAISE EXCEPTION
                    'Feature type % is non-hierarchical and cannot have a parent',
                    v_type_slug;
            END IF;

            -- Parent admin_level must be strictly less than child admin_level
            IF NEW.parent_id IS NOT NULL AND v_admin_level IS NOT NULL THEN
                SELECT ftc.admin_level INTO v_parent_admin_lvl
                  FROM curated_geo.feature f
                  JOIN curated_geo.feature_type_catalog ftc ON ftc.id = f.type_id
                 WHERE f.id = NEW.parent_id;
                IF v_parent_admin_lvl IS NOT NULL AND v_parent_admin_lvl >= v_admin_level THEN
                    RAISE EXCEPTION
                        'Parent admin_level (%) must be < child admin_level (%)',
                        v_parent_admin_lvl, v_admin_level;
                END IF;
            END IF;

            -- Reject recursive cycles in parent chains.
            IF NEW.parent_id IS NOT NULL AND NEW.id IS NOT NULL THEN
                WITH RECURSIVE parent_chain AS (
                    SELECT f.id, f.parent_id
                    FROM curated_geo.feature f
                    WHERE f.id = NEW.parent_id

                    UNION

                    SELECT f.id, f.parent_id
                    FROM curated_geo.feature f
                    JOIN parent_chain pc ON pc.parent_id = f.id
                )
                SELECT EXISTS (
                    SELECT 1
                    FROM parent_chain
                    WHERE id = NEW.id
                )
                INTO v_cycle_found;

                IF v_cycle_found THEN
                    RAISE EXCEPTION 'Feature hierarchy cycle detected for id=%', NEW.id;
                END IF;
            END IF;

            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER feature_before_write
            BEFORE INSERT OR UPDATE ON curated_geo.feature
            FOR EACH ROW EXECUTE FUNCTION curated_geo.feature_before_write()
    """)

    # ── observation ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.observation (
            id               uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            feature_id       uuid             NOT NULL
                                              REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
            attribute_key    text             NOT NULL CHECK (attribute_key <> ''),
            value_type       text             NOT NULL
                                              CHECK (value_type IN ('numeric','text','json')),
            value_numeric    double precision,
            value_text       text,
            value_json       jsonb,
            source_id        uuid             NOT NULL
                                              REFERENCES curated_geo.data_source_catalog(id),
            ingestion_run_id uuid             NOT NULL
                                              REFERENCES curated_geo.ingestion_run(id),
            lineage_ref      jsonb,
            is_current       boolean          NOT NULL DEFAULT true,
            valid_from       timestamptz      NOT NULL DEFAULT now(),
            valid_to         timestamptz,
            created_at       timestamptz      NOT NULL DEFAULT now(),
            updated_at       timestamptz      NOT NULL DEFAULT now(),
            CHECK ((is_current AND valid_to IS NULL) OR (NOT is_current AND valid_to IS NOT NULL)),
            CHECK (valid_to IS NULL OR valid_to >= valid_from)
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX obs_one_current
            ON curated_geo.observation (feature_id, attribute_key)
            WHERE is_current
    """)
    op.execute("""
        CREATE INDEX obs_feature_key_idx ON curated_geo.observation (feature_id, attribute_key)
    """)
    op.execute("""
        CREATE INDEX obs_source_run_idx ON curated_geo.observation (source_id, ingestion_run_id)
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.observation_before_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_run_source_id uuid;
            v_value_count   int;
        BEGIN
            -- Exactly one value column must be populated
            v_value_count := (NEW.value_numeric IS NOT NULL)::int
                           + (NEW.value_text    IS NOT NULL)::int
                           + (NEW.value_json    IS NOT NULL)::int;
            IF v_value_count <> 1 THEN
                RAISE EXCEPTION
                    'Exactly one of value_numeric, value_text, value_json must be set (got %)',
                    v_value_count;
            END IF;

            -- value_type must match the populated column
            IF NEW.value_type = 'numeric' AND NEW.value_numeric IS NULL THEN
                RAISE EXCEPTION 'value_type=numeric requires value_numeric to be set';
            END IF;
            IF NEW.value_type = 'text' AND NEW.value_text IS NULL THEN
                RAISE EXCEPTION 'value_type=text requires value_text to be set';
            END IF;
            IF NEW.value_type = 'json' AND NEW.value_json IS NULL THEN
                RAISE EXCEPTION 'value_type=json requires value_json to be set';
            END IF;

            -- source_id must match ingestion_run.source_id
            SELECT source_id INTO v_run_source_id
              FROM curated_geo.ingestion_run WHERE id = NEW.ingestion_run_id;
            IF v_run_source_id IS DISTINCT FROM NEW.source_id THEN
                RAISE EXCEPTION
                    'source_id mismatch: observation.source_id=% but ingestion_run.source_id=%',
                    NEW.source_id, v_run_source_id;
            END IF;

            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER observation_before_write
            BEFORE INSERT OR UPDATE ON curated_geo.observation
            FOR EACH ROW EXECUTE FUNCTION curated_geo.observation_before_write()
    """)

    # ── file_asset ───────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE curated_geo.file_asset (
            id                    uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id             uuid             NOT NULL
                                                   REFERENCES curated_geo.data_source_catalog(id) UNIQUE,
            ingestion_run_id      uuid             NOT NULL
                                                   REFERENCES curated_geo.ingestion_run(id),
            storage_uri           text             NOT NULL,
            format                text
                                                   CHECK (format IN
                                                     ('COG','GeoTIFF','NetCDF','LAZ','JPEG','PNG')),
            crs                   text,
            resolution_m          double precision,
            band_description      jsonb            NOT NULL DEFAULT '{}',
            asset_extent_geometry geometry(Polygon, 4326),
            captured_at           timestamptz,
            dataset_version       text             NOT NULL,
            created_at            timestamptz      NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION curated_geo.file_asset_before_write()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_storage_type text;
            v_run_source_id uuid;
        BEGIN
            SELECT storage_type INTO v_storage_type
              FROM curated_geo.data_source_catalog WHERE id = NEW.source_id;
            IF v_storage_type NOT IN ('raster','imagery','point_cloud') THEN
                RAISE EXCEPTION
                    'file_asset requires storage_type in (raster, imagery, point_cloud), got %',
                    v_storage_type;
            END IF;

            SELECT source_id INTO v_run_source_id
              FROM curated_geo.ingestion_run WHERE id = NEW.ingestion_run_id;
            IF v_run_source_id IS DISTINCT FROM NEW.source_id THEN
                RAISE EXCEPTION
                    'source_id mismatch: file_asset.source_id=% but ingestion_run.source_id=%',
                    NEW.source_id, v_run_source_id;
            END IF;
            RETURN NEW;
        END;
        $$
    """)

    op.execute("""
        CREATE TRIGGER file_asset_before_write
            BEFORE INSERT OR UPDATE ON curated_geo.file_asset
            FOR EACH ROW EXECUTE FUNCTION curated_geo.file_asset_before_write()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.file_asset CASCADE")
    op.execute("DROP FUNCTION IF EXISTS curated_geo.file_asset_before_write CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.observation CASCADE")
    op.execute("DROP FUNCTION IF EXISTS curated_geo.observation_before_write CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.feature CASCADE")
    op.execute("DROP FUNCTION IF EXISTS curated_geo.feature_before_write CASCADE")
    op.execute("DROP TABLE IF EXISTS curated_geo.feature_type_catalog CASCADE")
