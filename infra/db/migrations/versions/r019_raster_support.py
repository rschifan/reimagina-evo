"""raster_layer registry + serving/explore views for raster tile layers.

Introduces `curated_geo.raster_layer` — the DB-backed registry that lets
`resolve_tile_sources()` emit raster-mode tile entries for `/tile-sources`
without hand-editing `infra/tiles/layers.json` per raster. Ingest
populates it; publish/unpublish flips a boolean.

Paired read surfaces:
- `serving_geo.raster_layer_v` — consumed by the tile resolver + the
  raster zonal-stats worker (workers read only from `serving_geo`).
- `explore.raster_layer_v` — engineer inspection, matching the existing
  `explore.*_v` conventions.

Postgres-specific note: `curated_geo.file_asset.source_id` is already
`UNIQUE` per r003 (`REFERENCES ... UNIQUE`). The matching DuckDB
uniqueness lives in the r019 DuckDB mirror via `CREATE UNIQUE INDEX`
since DuckDB's `ALTER TABLE` can't add a UNIQUE constraint to an
existing table.

`aggregation_kind` carries the intensive/extensive distinction used by
the zonal-stats worker to pick default statistics. Categorical rasters
are out of v1 scope; adding them later means widening this CHECK.

Revision ID: r019
Revises: r018
Create Date: 2026-04-23
"""
from __future__ import annotations

from alembic import op


revision = "r019"
down_revision = "r018"
branch_labels = None
depends_on = None


_NEW_CATALOG_ROWS = [
    # (schema_name, object_name, logical_name, layer_kind, description,
    #  primary_geometry_column, intended_use, display_order)
    ("curated_geo", "raster_layer", "raster_layers", "canonical",
     "Registry of published raster tile layers (one row per registered raster asset)", None,
     "Inspect registered rasters + their publish status + aggregation_kind", 55),
    ("serving_geo", "raster_layer_v", "raster_layers_serving", "serving",
     "App-facing view of raster_layer joined with file_asset metadata (published rasters only)", None,
     "Inspect what /tile-sources and the zonal-stats worker consume", 115),
    ("explore", "raster_layer_v", "raster_layer_catalog", "explore",
     "Engineer view: published raster layers + source/version + storage URI", None,
     "Audit registered rasters + debug tile-source resolution", 940),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.raster_layer (
            id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id         uuid        NOT NULL UNIQUE
                                          REFERENCES curated_geo.data_source_catalog(id) ON DELETE CASCADE,
            asset_id          uuid        NOT NULL UNIQUE
                                          REFERENCES curated_geo.file_asset(id) ON DELETE CASCADE,
            layer_slug        text        NOT NULL UNIQUE,
            logical_name      text,
            description       text,
            colormap_name     text,
            rescale           text,
            minzoom           integer     NOT NULL DEFAULT 5,
            maxzoom           integer     NOT NULL DEFAULT 14,
            published         boolean     NOT NULL DEFAULT true,
            aggregation_kind  text        NOT NULL DEFAULT 'intensive'
                                          CHECK (aggregation_kind IN ('intensive', 'extensive')),
            created_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX raster_layer_published_idx
            ON curated_geo.raster_layer (published)
            WHERE published
    """)

    # Serving view — workers + API read here, not the canonical table.
    op.execute("""
        CREATE OR REPLACE VIEW serving_geo.raster_layer_v AS
        SELECT
            r.id,
            r.source_id,
            r.asset_id,
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
            a.band_description
        FROM curated_geo.raster_layer r
        JOIN curated_geo.file_asset a ON a.id = r.asset_id
    """)

    # Explore view — engineer inspection surface.
    op.execute("""
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
        JOIN curated_geo.file_asset a          ON a.id = r.asset_id
    """)

    for row in _NEW_CATALOG_ROWS:
        op.execute(
            "INSERT INTO explore.layer_catalog "
            "(schema_name, object_name, logical_name, layer_kind, description, "
            "primary_geometry_column, intended_use, display_order) VALUES "
            f"('{row[0]}', '{row[1]}', '{row[2]}', '{row[3]}', '{row[4]}', "
            f"{_sql_null_or(row[5])}, '{row[6]}', {row[7]})"
        )


def _sql_null_or(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def downgrade() -> None:
    for row in _NEW_CATALOG_ROWS:
        op.execute(
            f"DELETE FROM explore.layer_catalog "
            f"WHERE schema_name = '{row[0]}' AND object_name = '{row[1]}'"
        )
    op.execute("DROP VIEW IF EXISTS explore.raster_layer_v")
    op.execute("DROP VIEW IF EXISTS serving_geo.raster_layer_v")
    op.execute("DROP INDEX IF EXISTS curated_geo.raster_layer_published_idx")
    op.execute("DROP TABLE IF EXISTS curated_geo.raster_layer")
