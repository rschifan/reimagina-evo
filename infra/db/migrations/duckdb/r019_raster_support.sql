-- r019: raster_layer registry + serving/explore views for raster tile layers.
--
-- Paired with infra/db/migrations/versions/r019_raster_support.py. DuckDB
-- does not support ALTER TABLE ... ADD UNIQUE on existing tables, so we
-- enforce the file_asset.source_id uniqueness (present in Postgres r003
-- as REFERENCES ... UNIQUE) via CREATE UNIQUE INDEX here. On fresh DBs
-- this is equivalent; on pre-r019 DuckDB databases with duplicate
-- source_ids, the index creation will fail loudly — that's the desired
-- signal, since the existing data violates an invariant the plan
-- assumes.

CREATE UNIQUE INDEX IF NOT EXISTS file_asset_source_uk
    ON curated_geo.file_asset (source_id);

CREATE TABLE IF NOT EXISTS curated_geo.raster_layer (
    id                UUID        PRIMARY KEY DEFAULT uuid(),
    source_id         UUID        NOT NULL UNIQUE
                                  REFERENCES curated_geo.data_source_catalog(id),
    asset_id          UUID        NOT NULL UNIQUE
                                  REFERENCES curated_geo.file_asset(id),
    layer_slug        VARCHAR     NOT NULL UNIQUE,
    logical_name      VARCHAR,
    description       VARCHAR,
    colormap_name     VARCHAR,
    rescale           VARCHAR,
    minzoom           INTEGER     NOT NULL DEFAULT 5,
    maxzoom           INTEGER     NOT NULL DEFAULT 14,
    published         BOOLEAN     NOT NULL DEFAULT true,
    aggregation_kind  VARCHAR     NOT NULL DEFAULT 'intensive',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (aggregation_kind IN ('intensive', 'extensive'))
);

-- DuckDB has no partial indexes; use a full one. The predicate is cheap
-- anyway at prototype scale.
CREATE INDEX IF NOT EXISTS raster_layer_published_idx
    ON curated_geo.raster_layer (published);

CREATE OR REPLACE VIEW serving_geo.raster_layer_v AS
SELECT
    CAST(r.id AS VARCHAR)        AS id,
    CAST(r.source_id AS VARCHAR) AS source_id,
    CAST(r.asset_id AS VARCHAR)  AS asset_id,
    r.layer_slug,
    r.logical_name,
    r.description,
    r.colormap_name,
    r.rescale,
    r.minzoom,
    r.maxzoom,
    r.published,
    r.aggregation_kind,
    r.created_at,
    a.storage_uri,
    a.crs,
    a.resolution_m,
    a.format,
    CAST(a.band_description AS VARCHAR) AS band_description
FROM curated_geo.raster_layer r
JOIN curated_geo.file_asset a ON a.id = r.asset_id;

CREATE OR REPLACE VIEW explore.raster_layer_v AS
SELECT
    r.layer_slug,
    r.published,
    r.aggregation_kind,
    r.colormap_name,
    r.rescale,
    r.minzoom,
    r.maxzoom,
    s.name    AS source_name,
    s.version AS source_version,
    a.format,
    a.crs,
    a.resolution_m,
    a.storage_uri
FROM curated_geo.raster_layer r
JOIN curated_geo.data_source_catalog s ON s.id = r.source_id
JOIN curated_geo.file_asset a          ON a.id = r.asset_id;

INSERT OR IGNORE INTO explore.layer_catalog
    (schema_name, object_name, logical_name, layer_kind, description,
     primary_geometry_column, intended_use, display_order)
VALUES
    ('curated_geo', 'raster_layer', 'raster_layers', 'canonical',
     'Registry of published raster tile layers (one row per registered raster asset)', NULL,
     'Inspect registered rasters + their publish status + aggregation_kind', 55),
    ('serving_geo', 'raster_layer_v', 'raster_layers_serving', 'serving',
     'App-facing view of raster_layer joined with file_asset metadata (published rasters only)', NULL,
     'Inspect what /tile-sources and the zonal-stats worker consume', 115),
    ('explore', 'raster_layer_v', 'raster_layer_catalog', 'explore',
     'Engineer view: published raster layers + source/version + storage URI', NULL,
     'Audit registered rasters + debug tile-source resolution', 940);
