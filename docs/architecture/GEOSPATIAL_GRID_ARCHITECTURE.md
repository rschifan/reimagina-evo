# Geospatial Grid Architecture for Urban Analytics and Scenario Modeling

A production-grade reference guide for modeling raster and tessellation-based data alongside canonical spatial features, observations, and scenario outputs.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Core Design Principles](#2-core-design-principles)
3. [Architecture Overview](#3-architecture-overview)
4. [Decision Guide — What to Use When](#4-decision-guide--what-to-use-when)
5. [The Four Data Paths](#5-the-four-data-paths)
6. [Spatial Reference Layer](#6-spatial-reference-layer)
7. [Case A — H3 Hierarchical Tessellation](#7-case-a--h3-hierarchical-tessellation)
8. [Case B — Fishnet / External Raster Grid (e.g. GHS-POP)](#8-case-b--fishnet--external-raster-grid-eg-ghs-pop)
9. [Complete Schema Reference](#9-complete-schema-reference)
10. [Data Flow Patterns](#10-data-flow-patterns)
11. [Scenario Modeling Mechanics](#11-scenario-modeling-mechanics)
12. [AOI Filtering Mechanics](#12-aoi-filtering-mechanics)
13. [Provenance and Reproducibility](#13-provenance-and-reproducibility)
14. [Core Query Patterns](#14-core-query-patterns)
15. [Performance Guidelines](#15-performance-guidelines)
16. [Anti-Patterns to Avoid](#16-anti-patterns-to-avoid)
17. [Real-World References](#17-real-world-references)

---

## 1. Purpose and Scope

This document defines the architecture for handling all types of spatial data in a production urban analytics platform, specifically addressing the tension between:

- **Discrete named entities** (features): blocks, roads, lots, bus stops, trees — each with identity, geometry, and a lifecycle
- **Continuous gridded data** (rasters): heat surfaces, flood depth, NDVI, accessibility — values defined everywhere over a continuous field
- **Tessellated cell outputs** (grids): indicator results returned as arrays of cells rather than per-feature values

The goal is a single coherent architecture that handles all three without forcing any of them into the wrong storage model.

### What this document covers

- When to use H3 tessellation vs. an external fishnet grid (e.g. GHS-POP)
- How to model, store, query, and version each data type
- How scenario modeling and AOI filtering work for each path
- How to trace provenance for any output value back to its source

### What this document does not cover

- Tile serving or map rendering pipelines (MVT, raster tiles)
- Real-time or streaming ingestion
- Distributed compute (Spark/Sedona) — referenced where relevant but not detailed

---

## 2. Core Design Principles

These principles drive every architectural decision. When in doubt, return to them.

### P1 — Match storage to query pattern, not data origin

The question is never "is this data a raster?" — it is "what is the primary access pattern and what is the cardinality?" A raster whose values are only ever queried per-block belongs in a zonal summary table, not a cell table. A model output that is always queried per-cell belongs in a cell table, not a raster file.

### P2 — Geometry is expensive; keys are cheap

Spatial predicates (`ST_Intersects`, `ST_Within`) at query time on large tables are the leading cause of geospatial performance failures. Pre-compute all spatial relationships at data registration time. At query time, use B-tree joins on integer or text keys.

### P3 — Scenarios are explicit dimensions, not soft filters

Every output row must carry `scenario_id` as part of its primary key. There must be no ambiguity about which scenario state produced which value. Baseline data is never overwritten; scenarios produce new rows.

### P4 — Provenance is structural, not procedural

Every computed output must trace back to a `model_run` record that captures the code version, parameters, scenario state, and input files used. Reproducibility must be achievable from the database record alone, without relying on external documentation.

### P5 — The architecture is grid-agnostic

No grid type (H3, S2, fishnet, GHS-POP) is privileged by the schema. All grids register through the same `grid` table and participate in the same query patterns via `feature_cell_membership` and `aoi_cell_membership`. The cell identifier format is the only thing that changes between grid types.

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           DATA SOURCES                                  │
│   field sensors │ remote sensing │ model outputs │ administrative data  │
└──────┬──────────────────┬─────────────────┬──────────────┬─────────────┘
       │                  │                 │              │
       ▼                  ▼                 ▼              ▼
┌────────────┐   ┌───────────────┐   ┌──────────┐   ┌──────────────────┐
│  FEATURE   │   │  RASTER /     │   │  CELL    │   │  PROVENANCE      │
│  PATH      │   │  COVERAGE     │   │  PATH    │   │  LAYER           │
│            │   │  PATH         │   │          │   │                  │
│ feature    │   │ raster_asset  │   │ grid     │   │ model_run        │
│ observation│   │ (STAC-aligned)│   │ cell_    │   │ scenario         │
│ indicator_ │   │ COG / Zarr    │   │ indicator│   │ scenario_        │
│ result     │   │ object store  │   │ _result  │   │ override         │
└──────┬─────┘   └──────┬────────┘   └────┬─────┘   └──────────────────┘
       │                │                 │
       └────────────────┴─────────────────┘
                        │
            SPATIAL BRIDGE LAYER
            feature_cell_membership
            aoi_cell_membership
                        │
            ┌───────────┴────────────┐
            │    QUERY / SERVE       │
            │                        │
            │  PostgreSQL / PostGIS  │  ← transactional, relational queries
            │  COG HTTP range reads  │  ← raster pixel access
            └────────────────────────┘
```

### The three paths always in play

| Path | Data type | Primary store | DB representation |
|---|---|---|---|
| Feature path | Named discrete entities | PostgreSQL / PostGIS | `feature`, `observation`, `indicator_result` |
| Raster path | Dense continuous fields | Object storage (COG/Zarr) | `raster_asset` + zonal summaries in `indicator_result` |
| Cell path | Sparse or dense grid outputs | PostgreSQL (sparse) + object storage (dense) | `cell_indicator_result` |

---

## 4. Decision Guide — What to Use When

This section is the practical entry point. Start here with a new dataset or model output.

### Step 1 — Identify the data type

```
Is the data defined everywhere over a continuous spatial field
(temperature, flood depth, NDVI, elevation)?
    YES → go to Step 2 (Raster path)
    NO  → go to Step 3
```

```
Step 3 — Does the data describe a named, stable, discrete entity
(block, road, lot, bus stop, tree, administrative zone)?
    YES → Feature path. Store geometry in `feature`.
          Store values in `observation` or `indicator_result`.
    NO  → go to Step 4
```

```
Step 4 — Is the data returned as a grid of cells
(model outputs per H3 cell, per fishnet cell, per GHS-POP pixel)?
    YES → Cell path. Go to Step 5.
```

```
Step 5 — What is the grid type?
    ├── Analytical tessellation with no external source → go to Step 6
    └── Grid aligned to an external dataset (GHS-POP, census grid,
        custom fishnet) → Case B (Fishnet Grid). See Section 8.
```

```
Step 6 — Do you need hierarchical aggregation
(zoom between resolution levels, compact/uncompact cells)?
    YES → Case A (H3). See Section 7.
    NO  → Either H3 or fishnet is fine. H3 is preferred for
          city-scale analytics where no external grid alignment
          is required.
```

### Quick reference table

| Situation | Recommended path | Section |
|---|---|---|
| Dense raster, queried per-feature (mean flood depth per block) | Raster path → zonal summary as `indicator_result` | 5.2 |
| Dense raster, cell-level queries needed | Raster path (file) + Cell path (sparse materialization) | 5.2, 5.3 |
| Sparse model output over H3 grid | Cell path, Case A | 7 |
| Output must align with GHS-POP, census grid, or any external raster | Cell path, Case B | 8 |
| Named administrative zone (census tract, district) | Feature path | 5.1 |
| Time series of sensor readings at a location | Feature path, `observation` table | 5.1 |
| Scenario comparison at cell level | Cell path, both cases | 11 |
| Accessibility or walkability surface | Raster path (source) + Cell path (H3 outputs) | 5.2, 7 |
| Population-weighted indicator | Cell path aligned to GHS-POP | 8 |

### H3 vs. Fishnet decision

| Criterion | Choose H3 | Choose Fishnet / External Grid |
|---|---|---|
| Grid is derived from an external dataset | No | Yes — align to source |
| Hierarchical aggregation between resolutions is needed | Yes | No (irregular) |
| Integration with routing or mobility data (Uber H3 ecosystem) | Yes | No |
| Cells must align with pixels in a source raster | No | Yes |
| Population weighting uses GHS-POP pixels | No | Yes |
| Sharing results with partners who use the same grid | Depends on partner tooling | Yes if they use same grid |
| H3 library available in your tech stack | Yes | Not required |

---

## 5. The Four Data Paths

### 5.1 Feature Path

**Use for:** Any spatial entity with stable identity that accumulates observations or receives indicator outputs over time.

**Examples:** city blocks, road segments, parcels, parks, bus stops, trees, administrative zones, census tracts.

**Key tables:** `feature`, `observation`, `indicator_result`

**Rules:**
- Every feature has a stable `id` (UUID) that never changes
- Geometry is stored once in `feature.geometry` (PostGIS)
- Measured values are stored in `observation` (timestamped, source-linked)
- Computed model outputs are stored in `indicator_result` (scenario-aware, run-linked)
- Features are never deleted; use a `deprecated_at` timestamp for retired entities

### 5.2 Raster Path

**Use for:** Dense continuous fields where the primary artifact is a georeferenced array — heat stress surfaces, flood depth, NDVI, accessibility isochrones, elevation.

**Rules:**
- The raster file itself is stored in object storage as a Cloud Optimized GeoTIFF (COG) or Zarr store
- The `raster_asset` table holds the catalog record (STAC-aligned metadata)
- Feature-level summaries (mean, max, p90 per block) are extracted asynchronously and written as `indicator_result` rows on the feature path
- Cell-level values are only materialized into `cell_indicator_result` if cell-level SQL queries are required; otherwise serve directly from the COG via HTTP range requests
- The raster file is the single source of truth; the DB holds only derived summaries

### 5.3 Cell Path

**Use for:** Indicator outputs whose native unit is a grid cell — not a named feature. Covers both H3 tessellations (Case A) and fishnet/external grids (Case B).

**Rules:**
- Only cells with non-null values are stored (sparse by design)
- Cell geometry is never stored — it is computed from the cell identifier and the grid's geotransform at read time
- Every row carries `scenario_id` and `run_id` as non-nullable columns
- Cross-path aggregation to features is performed via `feature_cell_membership`, never via a live spatial join

### 5.4 Provenance Path

**Use for:** Tracking which code, parameters, inputs, and scenario state produced any given output row.

**Rules:**
- Every computed output (`indicator_result`, `cell_indicator_result`, `raster_asset`) references a `model_run` row via `run_id`
- `model_run` stores the full parameter snapshot as JSONB — not a pointer to a config file
- Input raster assets are recorded as an array of UUIDs (`input_asset_ids`) on `model_run`
- Checksums on `raster_asset` provide integrity verification for reproducibility

---

## 6. Spatial Reference Layer

This layer is written once at deployment time and rarely changes. It defines the coordinate systems and tessellation schemas used everywhere else.

```sql
-- Canonical H3 resolutions in use for this deployment.
-- Only populated when H3 grids are used (Case A).
CREATE TABLE h3_resolution_config (
    resolution       int PRIMARY KEY,    -- H3 resolution level (0–15)
    label            text NOT NULL,      -- human label: 'district', 'block', 'parcel'
    avg_area_m2      double precision,   -- approximate cell area in m²
    use_for_features boolean DEFAULT false,
    use_for_cells    boolean DEFAULT false
);

-- Example values for a city-scale deployment
INSERT INTO h3_resolution_config VALUES
    (7,  'district',    5161000, true,  false),
    (8,  'subdistrict',  737000, true,  false),
    (9,  'block',        105000, true,  true),
    (10, 'parcel',        15000, false, false);


-- Master grid registry — accommodates H3, S2, fishnet, and custom grids
CREATE TABLE grid (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    type            text NOT NULL
                    CHECK (type IN ('h3', 's2', 'fishnet', 'custom')),

    -- H3 / S2 fields (null for fishnet)
    resolution      int,

    -- Fishnet / raster-aligned fields (null for H3/S2)
    origin_x        double precision,   -- top-left x in CRS units
    origin_y        double precision,   -- top-left y in CRS units
    pixel_size_x    double precision,   -- cell width in CRS units (positive eastward)
    pixel_size_y    double precision,   -- cell height in CRS units (positive, applied northward)
    n_rows          int,
    n_cols          int,

    -- Common fields
    crs             text NOT NULL DEFAULT 'EPSG:4326',
    aoi_id          uuid,
    cell_count      bigint,
    source_asset_id uuid,               -- FK to raster_asset if grid is derived from a raster
    metadata        jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fishnet_requires_geotransform CHECK (
        type != 'fishnet' OR (
            origin_x IS NOT NULL AND origin_y IS NOT NULL AND
            pixel_size_x IS NOT NULL AND pixel_size_y IS NOT NULL AND
            n_rows IS NOT NULL AND n_cols IS NOT NULL
        )
    )
);
```

---

## 7. Case A — H3 Hierarchical Tessellation

### 7.1 When to use H3

Use H3 when:
- You are constructing an analytical grid from scratch (not aligning to an external dataset)
- You need hierarchical aggregation between resolution levels (compact/uncompact, parent/child cells)
- Your platform integrates with mobility or routing data (H3 is the industry standard for this use case)
- You want the cell identifier itself to encode spatial position (no separate geometry lookup required for approximate location)

**Do not use H3 when** the model that produces outputs is tied to an external raster grid (GHS-POP, land cover, DEM) and cell alignment with that raster is required. Resampling to H3 introduces interpolation error. Use Case B instead.

### 7.2 H3 Resolution Reference

| Resolution | Label | Avg area | Typical city cell count | Recommended use |
|---|---|---|---|---|
| 6 | Metro region | 36 km² | ~50–200 | Regional summaries |
| 7 | District | 5.2 km² | ~500–2K | District-level analytics |
| 8 | Sub-district | 0.74 km² | ~3K–15K | Neighborhood analytics |
| 9 | Block | 0.1 km² | ~25K–100K | Block-level, recommended for most city analytics |
| 10 | Parcel | 15K m² | ~200K–1M | Fine-grained; only if query volume justifies it |
| 11+ | Sub-parcel | < 3K m² | > 1M | Do not put in relational DB |

**Rule of thumb:** Use resolution 9 as the primary analytical resolution. Pre-aggregate to resolution 7–8 for dashboards and regional summaries. Never store resolution 11+ in PostgreSQL.

### 7.3 Schema — H3 Case

```sql
-- Augment the feature table with H3 index columns.
-- These are populated by a trigger on INSERT/UPDATE.
ALTER TABLE feature
    ADD COLUMN h3_res8 text,   -- H3 index at resolution 8
    ADD COLUMN h3_res9 text;   -- H3 index at resolution 9

-- Index for cross-path joins (cell → feature lookup)
CREATE INDEX ON feature (h3_res9);
CREATE INDEX ON feature (h3_res8);

-- For polygon features that span multiple H3 cells.
-- Stores the set of H3 cells overlapped by the feature geometry.
-- Populated once at registration; updated when geometry changes.
CREATE TABLE feature_cell_membership (
    feature_id  uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    grid_id     uuid NOT NULL REFERENCES grid(id),
    cell_id     text NOT NULL,               -- H3 index string at the grid's resolution
    coverage    double precision             -- fraction of the H3 cell covered by the feature (0–1)
                CHECK (coverage > 0 AND coverage <= 1),
    PRIMARY KEY (feature_id, grid_id, cell_id)
);
CREATE INDEX ON feature_cell_membership (grid_id, cell_id);
CREATE INDEX ON feature_cell_membership (feature_id, grid_id);

-- Trigger: auto-populate h3_res8 and h3_res9 on feature insert/update
CREATE OR REPLACE FUNCTION feature_set_h3_indices()
RETURNS trigger AS $$
BEGIN
    IF NEW.geometry IS NOT NULL THEN
        -- Requires the h3-pg extension: CREATE EXTENSION h3;
        NEW.h3_res8 := h3_lat_lng_to_cell(
            ST_Y(ST_Centroid(NEW.geometry)),
            ST_X(ST_Centroid(NEW.geometry)), 8);
        NEW.h3_res9 := h3_lat_lng_to_cell(
            ST_Y(ST_Centroid(NEW.geometry)),
            ST_X(ST_Centroid(NEW.geometry)), 9);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_feature_h3
    BEFORE INSERT OR UPDATE OF geometry ON feature
    FOR EACH ROW EXECUTE FUNCTION feature_set_h3_indices();
```

### 7.4 Registering an H3 Grid

```sql
-- Register a resolution-9 H3 grid covering a study area
INSERT INTO grid (name, type, resolution, crs, aoi_id, metadata)
VALUES (
    'h3_res9_city_a',
    'h3',
    9,
    'EPSG:4326',
    :aoi_id,
    '{"description": "Resolution 9 H3 grid for City A urban analytics"}'
);
```

**Population of `feature_cell_membership` for H3 (Python):**

```python
import h3
import psycopg2
from shapely.geometry import shape, mapping

def register_h3_grid_membership(conn, grid_id: str, resolution: int):
    """
    For each polygon feature in the DB, enumerate the H3 cells it covers
    and insert into feature_cell_membership.
    Run once per grid registration.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, ST_AsGeoJSON(geometry)::json
            FROM feature
            WHERE geometry IS NOT NULL
              AND ST_GeometryType(geometry) IN ('ST_Polygon', 'ST_MultiPolygon')
        """)
        features = cur.fetchall()

    rows = []
    for feature_id, geojson in features:
        geom = shape(geojson)
        # h3.polyfill returns all cells whose center is within the polygon
        cells = h3.polyfill_geojson(mapping(geom), resolution)
        for cell_id in cells:
            # Approximate coverage: cell area that intersects feature / cell area
            cell_poly = shape({"type": "Polygon", "coordinates": [
                h3.h3_to_geo_boundary(cell_id, geo_json=True)
            ]})
            coverage = geom.intersection(cell_poly).area / cell_poly.area
            rows.append((str(feature_id), grid_id, cell_id, coverage))

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO feature_cell_membership (feature_id, grid_id, cell_id, coverage)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, rows)
    conn.commit()
```

**Population of `aoi_cell_membership` for H3 (Python):**

```python
def register_h3_aoi_membership(conn, aoi_id: str, grid_id: str,
                                aoi_geojson: dict, resolution: int):
    """
    Enumerate all H3 cells within an AOI polygon and store membership.
    Run once per AOI per grid. Never re-run at query time.
    """
    cells = h3.polyfill_geojson(aoi_geojson, resolution)
    rows = [(aoi_id, grid_id, cell_id) for cell_id in cells]

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO aoi_cell_membership (aoi_id, grid_id, cell_id)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, rows)
    conn.commit()
    print(f"Registered {len(rows)} cells for AOI {aoi_id} at resolution {resolution}")
```

### 7.5 Cell ID format for H3

The H3 cell identifier is the H3 index string itself, e.g. `"882830829bfffff"`. It is compact, globally unique, and encodes the resolution and spatial position. No additional metadata is needed to reconstruct the cell's geometry.

```python
import h3

cell_id = "882830829bfffff"

# Reconstruct cell center
lat, lng = h3.h3_to_geo(cell_id)

# Reconstruct cell boundary (hexagon vertices)
boundary = h3.h3_to_geo_boundary(cell_id)

# Get parent cell (one resolution coarser)
parent = h3.h3_to_parent(cell_id, 8)   # → "882830829bfffff" parent at res 8

# Get children (one resolution finer)
children = h3.h3_to_children(cell_id, 10)
```

---

## 8. Case B — Fishnet / External Raster Grid (e.g. GHS-POP)

### 8.1 When to use an external grid

Use a fishnet or externally-defined grid when:
- Model outputs must spatially align with a source raster (GHS-POP population grid, land cover, DEM, satellite-derived products)
- Population weighting requires exact pixel alignment with GHS-POP
- You are combining outputs with datasets that use the same grid (e.g. European urban atlas at 100m)
- Partners or downstream tools consume data in a specific grid format

**Do not introduce a resampling step to H3 just for architectural uniformity.** Resampling a 100m GHS-POP grid to H3 hexagons introduces interpolation error and makes population-weighted calculations incorrect. Use the native grid.

### 8.2 GHS-POP Grid Reference

The Global Human Settlement Population (GHS-POP) grid is a standard reference dataset produced by the European Commission's Joint Research Centre (JRC).

| Parameter | Value |
|---|---|
| Resolutions available | 100m, 1km |
| Projections | EPSG:4326 (geographic), ESRI:54009 (Mollweide equal-area) |
| Format | GeoTIFF |
| Cell identifier | (row, col) from top-left origin |
| Coverage | Global |
| Epoch | 1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025, 2030 |

For urban analytics, use the **100m resolution in EPSG:4326** unless population-weighted area calculations require the equal-area Mollweide projection, in which case use ESRI:54009.

### 8.3 Schema — Fishnet / External Grid Case

The core `feature_cell_membership` and `aoi_cell_membership` tables are identical to Case A. The `grid` table gains geotransform fields. No H3-specific columns are needed on the `feature` table.

```sql
-- No H3 columns on feature for this case.
-- feature_cell_membership uses (row:col) as cell_id instead of H3 index.

CREATE TABLE feature_cell_membership (
    feature_id  uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    grid_id     uuid NOT NULL REFERENCES grid(id),
    cell_id     text NOT NULL,     -- format: "row:col" e.g. "42:137"
    coverage    double precision
                CHECK (coverage > 0 AND coverage <= 1),
    PRIMARY KEY (feature_id, grid_id, cell_id)
);
CREATE INDEX ON feature_cell_membership (grid_id, cell_id);
CREATE INDEX ON feature_cell_membership (feature_id, grid_id);
```

### 8.4 Registering a Fishnet Grid (GHS-POP example)

```sql
-- First, register the source raster as a raster_asset
INSERT INTO raster_asset (
    collection_id, title, storage_uri, storage_format,
    pixel_size_m, crs, is_baseline, band_metadata
)
VALUES (
    'ghs_pop',
    'GHS-POP R2023A 100m EPSG:4326 Epoch 2020',
    's3://your-bucket/reference/GHS_POP_E2020_GLOBE_R2023A_4326_100m.tif',
    'COG',
    100,
    'EPSG:4326',
    true,
    '{"band_1": {"name": "population", "unit": "inhabitants", "nodata": -200}}'
) RETURNING id;
-- save returned id as :ghspop_asset_id

-- Then register the derived grid
INSERT INTO grid (
    name, type,
    origin_x, origin_y,
    pixel_size_x, pixel_size_y,
    n_rows, n_cols,
    crs, aoi_id, source_asset_id, metadata
)
VALUES (
    'ghspop_100m_city_a',
    'fishnet',
    -74.0500,         -- top-left longitude of AOI bounding box
     40.9000,         -- top-left latitude of AOI bounding box
    0.00090090,       -- ~100m in degrees longitude at this latitude
    0.00090090,       -- ~100m in degrees latitude
    500,              -- number of rows covering the AOI
    600,              -- number of columns covering the AOI
    'EPSG:4326',
    :aoi_id,
    :ghspop_asset_id,
    '{"source": "JRC GHS-POP R2023A", "epoch": 2020, "resolution_m": 100}'
);
```

**How to derive origin and pixel_size from the GeoTIFF (Python):**

```python
import rasterio

def get_grid_params_from_raster(raster_path: str, aoi_bounds: tuple) -> dict:
    """
    Extract geotransform parameters from a GeoTIFF for registration.
    aoi_bounds: (west, south, east, north) in the raster's CRS
    """
    with rasterio.open(raster_path) as src:
        transform = src.transform
        # GDAL geotransform: (origin_x, pixel_size_x, 0, origin_y, 0, -pixel_size_y)
        # rasterio Affine: (pixel_size_x, 0, origin_x, 0, -pixel_size_y, origin_y)
        pixel_size_x = transform.a       # positive, eastward
        pixel_size_y = abs(transform.e)  # stored as negative in raster convention

        # Compute the row/col window for the AOI
        west, south, east, north = aoi_bounds
        window = rasterio.windows.from_bounds(
            west, south, east, north, transform
        )
        row_off = int(window.row_off)
        col_off = int(window.col_off)
        n_rows  = int(window.height)
        n_cols  = int(window.width)

        # Origin of the AOI sub-grid (top-left corner)
        origin_x = transform.c + col_off * pixel_size_x
        origin_y = transform.f - row_off * pixel_size_y

        return {
            "origin_x":    origin_x,
            "origin_y":    origin_y,
            "pixel_size_x": pixel_size_x,
            "pixel_size_y": pixel_size_y,
            "n_rows":      n_rows,
            "n_cols":      n_cols,
            "crs":         str(src.crs),
            "col_offset":  col_off,   # offset into the global raster (for reading)
            "row_offset":  row_off
        }
```

### 8.5 Cell ID format for Fishnet

```
cell_id = "row:col"

Examples:
  "0:0"       → top-left cell of the AOI grid
  "42:137"    → row 42, column 137
  "499:599"   → bottom-right cell of a 500×600 grid
```

**Cell ID to geographic bounding box (Python):**

```python
def cell_id_to_bbox(grid: dict, cell_id: str) -> tuple:
    """
    Convert a "row:col" cell ID to a geographic bounding box.
    Returns (west, south, east, north) in the grid's CRS.
    """
    row, col = map(int, cell_id.split(":"))
    x_min = grid["origin_x"] + col * grid["pixel_size_x"]
    y_max = grid["origin_y"] - row * grid["pixel_size_y"]
    x_max = x_min + grid["pixel_size_x"]
    y_min = y_max - grid["pixel_size_y"]
    return (x_min, y_min, x_max, y_max)

def cell_id_to_shapely(grid: dict, cell_id: str):
    from shapely.geometry import box
    return box(*cell_id_to_bbox(grid, cell_id))
```

**Cell ID to global raster row/col (for reading from GeoTIFF):**

```python
def cell_id_to_global_pixel(grid: dict, cell_id: str) -> tuple:
    """
    Convert local AOI cell_id to the pixel coordinates in the source raster.
    Use this when reading values from the original GHS-POP GeoTIFF.
    """
    local_row, local_col = map(int, cell_id.split(":"))
    global_row = local_row + grid["row_offset"]
    global_col = local_col + grid["col_offset"]
    return (global_row, global_col)
```

### 8.6 Population of `feature_cell_membership` for fishnet grids (Python)

```python
import rasterio
import geopandas as gpd
from shapely.geometry import box
from exactextract import exact_extract   # pip install exactextract

def register_fishnet_membership(conn, grid_id: str, grid_params: dict,
                                 features_gdf: gpd.GeoDataFrame):
    """
    For each polygon feature, find which fishnet cells it overlaps
    and compute the coverage fraction.
    Uses exactextract for accurate fractional coverage computation.
    """
    rows = []
    origin_x   = grid_params["origin_x"]
    origin_y   = grid_params["origin_y"]
    px         = grid_params["pixel_size_x"]
    py         = grid_params["pixel_size_y"]
    n_rows     = grid_params["n_rows"]
    n_cols     = grid_params["n_cols"]

    for _, feature in features_gdf.iterrows():
        geom = feature.geometry
        if geom is None or geom.is_empty:
            continue

        # Find candidate cells via bounding box
        bounds = geom.bounds  # (minx, miny, maxx, maxy)
        col_min = max(0, int((bounds[0] - origin_x) / px))
        col_max = min(n_cols - 1, int((bounds[2] - origin_x) / px))
        row_min = max(0, int((origin_y - bounds[3]) / py))
        row_max = min(n_rows - 1, int((origin_y - bounds[1]) / py))

        for r in range(row_min, row_max + 1):
            for c in range(col_min, col_max + 1):
                cell_box = box(
                    origin_x + c * px,
                    origin_y - (r + 1) * py,
                    origin_x + (c + 1) * px,
                    origin_y - r * py
                )
                intersection = geom.intersection(cell_box)
                if intersection.is_empty:
                    continue
                coverage = intersection.area / cell_box.area
                if coverage > 0:
                    cell_id = f"{r}:{c}"
                    rows.append((str(feature["id"]), grid_id, cell_id, coverage))

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO feature_cell_membership (feature_id, grid_id, cell_id, coverage)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, rows)
    conn.commit()
    print(f"Registered {len(rows)} feature-cell overlaps for grid {grid_id}")
```

### 8.7 Population of `aoi_cell_membership` for fishnet grids (Python)

```python
from shapely.geometry import box, shape

def register_fishnet_aoi_membership(conn, aoi_id: str, grid_id: str,
                                     grid_params: dict, aoi_geojson: dict):
    """
    Enumerate all fishnet cells within an AOI polygon.
    Uses bounding box pre-filter then exact intersection test.
    """
    aoi_geom = shape(aoi_geojson)
    origin_x = grid_params["origin_x"]
    origin_y = grid_params["origin_y"]
    px       = grid_params["pixel_size_x"]
    py       = grid_params["pixel_size_y"]
    n_rows   = grid_params["n_rows"]
    n_cols   = grid_params["n_cols"]

    bounds   = aoi_geom.bounds
    col_min  = max(0, int((bounds[0] - origin_x) / px))
    col_max  = min(n_cols - 1, int((bounds[2] - origin_x) / px))
    row_min  = max(0, int((origin_y - bounds[3]) / py))
    row_max  = min(n_rows - 1, int((origin_y - bounds[1]) / py))

    rows = []
    for r in range(row_min, row_max + 1):
        for c in range(col_min, col_max + 1):
            cell_box = box(
                origin_x + c * px,
                origin_y - (r + 1) * py,
                origin_x + (c + 1) * px,
                origin_y - r * py
            )
            if aoi_geom.intersects(cell_box):
                rows.append((aoi_id, grid_id, f"{r}:{c}"))

    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO aoi_cell_membership (aoi_id, grid_id, cell_id)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, rows)
    conn.commit()
    print(f"Registered {len(rows)} cells in AOI membership for grid {grid_id}")
```

---

## 9. Complete Schema Reference

All tables, with column-level comments.

```sql
-- ─────────────────────────────────────────────────────────────────
-- SPATIAL REFERENCE
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE h3_resolution_config (
    resolution        int PRIMARY KEY,
    label             text NOT NULL,
    avg_area_m2       double precision,
    use_for_features  boolean DEFAULT false,
    use_for_cells     boolean DEFAULT false
);

CREATE TABLE grid (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL UNIQUE,
    type            text NOT NULL CHECK (type IN ('h3', 's2', 'fishnet', 'custom')),
    -- H3 / S2 only
    resolution      int,
    -- Fishnet / raster-aligned only
    origin_x        double precision,
    origin_y        double precision,
    pixel_size_x    double precision,
    pixel_size_y    double precision,
    n_rows          int,
    n_cols          int,
    -- Common
    crs             text NOT NULL DEFAULT 'EPSG:4326',
    aoi_id          uuid,
    cell_count      bigint,
    source_asset_id uuid,
    metadata        jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fishnet_requires_geotransform CHECK (
        type != 'fishnet' OR (
            origin_x IS NOT NULL AND origin_y IS NOT NULL AND
            pixel_size_x IS NOT NULL AND pixel_size_y IS NOT NULL AND
            n_rows IS NOT NULL AND n_cols IS NOT NULL
        )
    )
);


-- ─────────────────────────────────────────────────────────────────
-- FEATURE PATH (existing tables, minimal additions)
-- ─────────────────────────────────────────────────────────────────

-- Existing table — add H3 index columns (Case A only)
ALTER TABLE feature
    ADD COLUMN IF NOT EXISTS h3_res8 text,
    ADD COLUMN IF NOT EXISTS h3_res9 text;
CREATE INDEX IF NOT EXISTS idx_feature_h3_res9 ON feature (h3_res9);

-- Existing table — add run_id for provenance
ALTER TABLE indicator_result
    ADD COLUMN IF NOT EXISTS run_id uuid REFERENCES model_run(id);


-- ─────────────────────────────────────────────────────────────────
-- RASTER / COVERAGE PATH
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE raster_asset (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stac_item_id    text UNIQUE,
    collection_id   text NOT NULL,           -- 'flood_depth', 'ghs_pop', 'ndvi'
    title           text,
    bbox            double precision[4],     -- [west, south, east, north] WGS84
    aoi_id          uuid,
    datetime_start  timestamptz,
    datetime_end    timestamptz,
    scenario_id     uuid,
    run_id          uuid,
    is_baseline     boolean NOT NULL DEFAULT false,
    storage_uri     text NOT NULL,           -- s3://bucket/path/file.tif
    storage_format  text NOT NULL
                    CHECK (storage_format IN ('COG', 'GeoTIFF', 'Zarr', 'GeoParquet')),
    byte_size       bigint,
    checksum_sha256 text,                    -- SHA-256 of the file for reproducibility
    crs             text,
    pixel_size_m    double precision,
    band_count      int DEFAULT 1,
    band_metadata   jsonb,                   -- per-band name, unit, nodata value
    links           jsonb,                   -- STAC links (thumbnail, overview, etc.)
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON raster_asset (collection_id, scenario_id);
CREATE INDEX ON raster_asset (run_id);


-- ─────────────────────────────────────────────────────────────────
-- PROVENANCE LAYER
-- ─────────────────────────────────────────────────────────────────

CREATE TABLE model_run (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name      text NOT NULL,
    model_version   text,
    scenario_id     uuid,
    parameters      jsonb NOT NULL DEFAULT '{}',   -- FULL parameter snapshot
    input_asset_ids uuid[],                        -- raster_asset IDs consumed
    input_run_ids   uuid[],                        -- upstream model_run IDs consumed
    triggered_by    text,                          -- 'user:alice', 'pipeline:nightly'
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'complete', 'failed')),
    error_message   text,
    started_at      timestamptz,
    completed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON model_run (scenario_id, model_name);
CREATE INDEX ON model_run (status, created_at);


-- ─────────────────────────────────────────────────────────────────
-- CELL PATH
-- ─────────────────────────────────────────────────────────────────

-- Sparse cell-level indicator outputs.
-- Only cells with non-null values are stored.
-- Partitioned by grid_id for query performance.
CREATE TABLE cell_indicator_result (
    grid_id         uuid NOT NULL REFERENCES grid(id),
    cell_id         text NOT NULL,
    scenario_id     uuid NOT NULL,
    indicator_id    uuid NOT NULL,
    value           double precision,
    value_json      jsonb,                          -- for vector or multi-band outputs
    run_id          uuid NOT NULL REFERENCES model_run(id),
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, cell_id, scenario_id, indicator_id)
) PARTITION BY LIST (grid_id);

-- Create one partition per registered grid:
-- CREATE TABLE cell_indicator_result_h3r9_city_a
--     PARTITION OF cell_indicator_result
--     FOR VALUES IN ('<grid_uuid>');

-- Cell-level scenario overrides.
-- Stores only the cells and variables that differ from baseline.
CREATE TABLE cell_scenario_override (
    grid_id         uuid NOT NULL REFERENCES grid(id),
    cell_id         text NOT NULL,
    scenario_id     uuid NOT NULL,
    variable_id     uuid NOT NULL,
    override_value  double precision,
    override_json   jsonb,
    rationale       text,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, cell_id, scenario_id, variable_id)
);


-- ─────────────────────────────────────────────────────────────────
-- SPATIAL BRIDGE LAYER
-- ─────────────────────────────────────────────────────────────────

-- Feature-to-cell overlap mapping.
-- Grid-agnostic: works for H3 and fishnet.
-- Populated ONCE at grid registration time, never at query time.
CREATE TABLE feature_cell_membership (
    feature_id  uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    grid_id     uuid NOT NULL REFERENCES grid(id),
    cell_id     text NOT NULL,
    coverage    double precision CHECK (coverage > 0 AND coverage <= 1),
    PRIMARY KEY (feature_id, grid_id, cell_id)
);
CREATE INDEX ON feature_cell_membership (grid_id, cell_id);
CREATE INDEX ON feature_cell_membership (feature_id, grid_id);

-- AOI-to-cell membership.
-- Populated ONCE at grid/AOI registration time, never at query time.
CREATE TABLE aoi_cell_membership (
    aoi_id   uuid NOT NULL,
    grid_id  uuid NOT NULL REFERENCES grid(id),
    cell_id  text NOT NULL,
    PRIMARY KEY (aoi_id, grid_id, cell_id)
);
CREATE INDEX ON aoi_cell_membership (grid_id, cell_id);
```

---

## 10. Data Flow Patterns

### Flow 1 — Dense raster input (flood model output, heat stress, NDVI)

```
1. Model run completes
   └─ Produces GeoTIFF on object storage

2. Write model_run row
   └─ status='complete', parameters={...}, scenario_id, input_asset_ids

3. Write raster_asset row
   └─ storage_uri, collection_id, scenario_id, run_id, checksum_sha256

4. Async zonal extraction job (exactextract or rasterstats)
   └─ For each feature in AOI: extract mean, max, p90 from raster
   └─ Write indicator_result rows (feature_id, scenario_id, indicator_id, value, run_id)

5. Optional: if cell-level SQL queries are required
   └─ Read raster values per cell using rasterio windowed reads
   └─ Write sparse cell_indicator_result rows (only cells with values)

Query time:
  - Feature analysts → indicator_result  (B-tree lookup)
  - Cell analysts    → cell_indicator_result (B-tree lookup)
  - Raster analysts  → fetch COG via storage_uri with HTTP range request
```

### Flow 2 — Sparse cell output (accessibility score, walkability index)

```
1. Model run completes
   └─ Produces dict: {cell_id: value} (only cells with computed values)

2. Write model_run row

3. Bulk insert cell_indicator_result
   └─ (grid_id, cell_id, scenario_id, indicator_id, value, run_id)
   └─ Sparse: only cells with values; table stays compact

4. Optional cross-path aggregation
   └─ JOIN cell_indicator_result WITH feature_cell_membership
   └─ Aggregate (weighted avg by coverage) per feature
   └─ Write indicator_result rows for feature-level summaries

5. AOI query:
   └─ Look up cell set in aoi_cell_membership
   └─ Filter cell_indicator_result by that set
```

### Flow 3 — External grid input (GHS-POP aligned indicator)

```
1. Register GHS-POP raster as raster_asset (once)

2. Register derived grid in grid table (once)
   └─ Store geotransform: origin_x/y, pixel_size_x/y, n_rows, n_cols

3. Run registration jobs (once per grid):
   a. register_fishnet_aoi_membership() → fills aoi_cell_membership
   b. register_fishnet_membership()     → fills feature_cell_membership

4. Model run produces {cell_id: value} aligned to this grid
   └─ Same insert flow as Flow 2

5. All queries use the same tables as Case A
   └─ Only the cell_id format differs: "row:col" vs H3 index string
```

---

## 11. Scenario Modeling Mechanics

### Baseline state

```sql
-- Baseline indicator results on features
SELECT * FROM indicator_result
WHERE scenario_id IS NULL   -- or WHERE scenario_id = :baseline_id
  AND indicator_id = :indicator_id;

-- Baseline cell results
SELECT * FROM cell_indicator_result
WHERE scenario_id = :baseline_id
  AND grid_id     = :grid_id
  AND indicator_id = :indicator_id;

-- Baseline raster assets
SELECT * FROM raster_asset
WHERE is_baseline = true
  AND collection_id = :collection_id;
```

### Running a scenario

A scenario never modifies baseline data. It produces new output rows tagged with `scenario_id`.

```python
def run_scenario(scenario_id: str, model_name: str, parameters: dict,
                 baseline_run_id: str) -> str:
    """
    Execute a model under a scenario, writing results as new rows.
    Returns the model_run ID.
    """
    run_id = create_model_run(
        model_name=model_name,
        scenario_id=scenario_id,
        parameters=parameters,
        input_run_ids=[baseline_run_id]
    )

    # Resolve scenario state: read baseline, apply overrides in memory
    baseline_cells = read_cell_indicator_result(
        scenario_id=BASELINE_ID, indicator_id=parameters["input_indicator"]
    )
    overrides = read_cell_scenario_overrides(scenario_id=scenario_id)

    effective_inputs = {**baseline_cells, **overrides}  # overrides win

    # Run the model
    outputs = model_function(effective_inputs, parameters)

    # Write results — new rows, not updates
    bulk_insert_cell_indicator_result(
        rows=[
            (GRID_ID, cell_id, scenario_id, OUTPUT_INDICATOR_ID, value, run_id)
            for cell_id, value in outputs.items()
        ]
    )

    mark_model_run_complete(run_id)
    return run_id
```

### Scenario comparison query

```sql
-- Which cells changed and by how much between baseline and scenario A?
SELECT
    base.cell_id,
    base.value                              AS baseline_value,
    scen.value                              AS scenario_value,
    scen.value - base.value                 AS delta,
    ROUND(
        (scen.value - base.value)
        / NULLIF(base.value, 0) * 100, 2
    )                                       AS pct_change
FROM cell_indicator_result base
JOIN cell_indicator_result scen
    ON  scen.cell_id      = base.cell_id
    AND scen.grid_id      = base.grid_id
    AND scen.indicator_id = base.indicator_id
WHERE base.grid_id       = :grid_id
  AND base.indicator_id  = :indicator_id
  AND base.scenario_id   = :baseline_id
  AND scen.scenario_id   = :scenario_id
  AND ABS(scen.value - base.value) > :min_delta
ORDER BY ABS(delta) DESC;
```

### What baseline scenario_id should be

Use a fixed well-known UUID registered in the `scenario` table as the baseline. Do not use `NULL`. Using `NULL` for baseline makes `scenario_id IN (baseline_id, scenario_a_id)` comparisons impossible and complicates indexing.

```sql
INSERT INTO scenario (id, name, description, is_baseline)
VALUES ('00000000-0000-0000-0000-000000000001', 'baseline', 'Observed current state', true);
```

---

## 12. AOI Filtering Mechanics

AOI filtering is the most common source of query performance problems in geospatial systems. The rule is: **all spatial membership is pre-computed at registration time, never computed at query time.**

### For the feature path

```sql
-- Feature path AOI filtering uses PostGIS spatial index — already in place
SELECT f.id, ir.value
FROM feature f
JOIN indicator_result ir ON ir.feature_id = f.id
WHERE ST_Within(f.geometry, :aoi_geometry)   -- spatial index on feature.geometry
  AND ir.scenario_id   = :scenario_id
  AND ir.indicator_id  = :indicator_id;

-- Alternative: if aoi_id is pre-linked to features (faster for repeated queries)
SELECT f.id, ir.value
FROM feature f
JOIN feature_aoi_membership fam ON fam.feature_id = f.id  -- pre-computed
JOIN indicator_result ir ON ir.feature_id = f.id
WHERE fam.aoi_id       = :aoi_id
  AND ir.scenario_id   = :scenario_id
  AND ir.indicator_id  = :indicator_id;
```

### For the cell path (both H3 and fishnet)

```sql
-- Cell path AOI filtering via pre-computed aoi_cell_membership
-- No geometry operations at query time
SELECT cir.cell_id, cir.value
FROM cell_indicator_result cir
JOIN aoi_cell_membership acm
    ON  acm.cell_id = cir.cell_id
    AND acm.grid_id = cir.grid_id
WHERE acm.aoi_id       = :aoi_id
  AND acm.grid_id      = :grid_id
  AND cir.scenario_id  = :scenario_id
  AND cir.indicator_id = :indicator_id;
-- Execution: index scan on aoi_cell_membership, nested loop to cell_indicator_result
-- No ST_* call anywhere in the query plan
```

### For the raster path

AOI filtering on rasters is done at the file level using GDAL window reads or HTTP range requests on COG files. No SQL is involved.

```python
import rasterio
from rasterio.mask import mask
from shapely.geometry import mapping

def read_raster_in_aoi(storage_uri: str, aoi_geom) -> tuple:
    """Read raster values within AOI using COG range requests."""
    with rasterio.open(storage_uri) as src:
        out_image, out_transform = mask(src, [mapping(aoi_geom)], crop=True)
    return out_image, out_transform
```

### Registration workflow (run once per grid, per AOI)

```
New grid registered
       │
       ├── H3 grid → h3.polyfill(aoi_geojson, resolution) → insert aoi_cell_membership
       │
       └── Fishnet grid → ST_Intersects loop → insert aoi_cell_membership

New AOI added for existing grid
       │
       └── Same process: enumerate cells, insert aoi_cell_membership rows
```

---

## 13. Provenance and Reproducibility

### Provenance chain

Every computed output traces back to a complete reconstruction path:

```
cell_indicator_result.run_id
    └─ model_run
          ├── model_name + model_version     → identifies the exact code
          ├── parameters (jsonb)             → full parameter snapshot
          ├── scenario_id → scenario
          │       └─ scenario_override[]     → what was changed from baseline
          ├── input_asset_ids[] → raster_asset[]
          │       └─ storage_uri             → exact input file
          │       └─ checksum_sha256         → file integrity guarantee
          └── input_run_ids[] → model_run[]  → upstream models consumed
```

### Full provenance query

```sql
-- Trace the full provenance of a specific cell value
SELECT
    cir.cell_id,
    cir.value,
    cir.computed_at,
    mr.model_name,
    mr.model_version,
    mr.parameters,
    s.name                   AS scenario_name,
    array_agg(DISTINCT ra.storage_uri)  AS input_rasters,
    array_agg(DISTINCT ra.checksum_sha256) AS input_checksums
FROM cell_indicator_result cir
JOIN model_run mr ON mr.id = cir.run_id
JOIN scenario s   ON s.id  = cir.scenario_id
LEFT JOIN raster_asset ra ON ra.id = ANY(mr.input_asset_ids)
WHERE cir.cell_id      = :cell_id
  AND cir.grid_id      = :grid_id
  AND cir.indicator_id = :indicator_id
GROUP BY cir.cell_id, cir.value, cir.computed_at,
         mr.model_name, mr.model_version, mr.parameters, s.name
ORDER BY cir.computed_at DESC;
```

### Reproducibility checklist

To reproduce any output row from scratch:

1. Read `model_run.parameters` — this is the complete parameter set
2. Read `model_run.model_version` — check out exactly this version of the model code
3. Reconstruct scenario state: read `scenario_override` rows for `model_run.scenario_id`
4. Fetch input files at `raster_asset.storage_uri` for each ID in `model_run.input_asset_ids`
5. Verify file integrity: `sha256sum file.tif` must match `raster_asset.checksum_sha256`
6. Re-run the model with the same parameters and scenario state

If any checksum fails, the input has changed since the run and the output cannot be guaranteed reproducible. Surface this as a data quality warning.

---

## 14. Core Query Patterns

### Q1 — All cell values in an AOI, for a scenario

```sql
-- Works identically for H3 and fishnet grids
SELECT
    cir.cell_id,
    cir.value
FROM cell_indicator_result cir
JOIN aoi_cell_membership acm
    ON  acm.cell_id = cir.cell_id
    AND acm.grid_id = cir.grid_id
WHERE acm.aoi_id       = :aoi_id
  AND acm.grid_id      = :grid_id
  AND cir.scenario_id  = :scenario_id
  AND cir.indicator_id = :indicator_id;
```

### Q2 — Feature-level aggregation from cell values (cross-path join)

```sql
-- Aggregate cell indicator values onto features using pre-computed coverage weights
SELECT
    fcm.feature_id,
    SUM(cir.value * fcm.coverage)
        / NULLIF(SUM(fcm.coverage), 0)           AS coverage_weighted_mean,
    MAX(cir.value)                               AS max_value,
    PERCENTILE_CONT(0.9) WITHIN GROUP
        (ORDER BY cir.value)                     AS p90_value,
    COUNT(*)                                     AS cell_count
FROM cell_indicator_result cir
JOIN feature_cell_membership fcm
    ON  fcm.cell_id = cir.cell_id
    AND fcm.grid_id = cir.grid_id
WHERE cir.grid_id      = :grid_id
  AND cir.scenario_id  = :scenario_id
  AND cir.indicator_id = :indicator_id
GROUP BY fcm.feature_id;
```

### Q3 — Scenario delta: cells that changed

```sql
SELECT
    base.cell_id,
    base.value                                        AS baseline_value,
    scen.value                                        AS scenario_value,
    scen.value - base.value                           AS delta,
    ROUND((scen.value - base.value)
          / NULLIF(base.value, 0) * 100, 2)           AS pct_change
FROM cell_indicator_result base
JOIN cell_indicator_result scen
    ON  scen.cell_id      = base.cell_id
    AND scen.grid_id      = base.grid_id
    AND scen.indicator_id = base.indicator_id
WHERE base.grid_id      = :grid_id
  AND base.indicator_id = :indicator_id
  AND base.scenario_id  = :baseline_id
  AND scen.scenario_id  = :scenario_id;
```

### Q4 — Population-weighted indicator (GHS-POP case)

```sql
-- When cell_id aligns with GHS-POP pixels,
-- join directly to GHS-POP cell values for population weighting.
-- Assumes population values are stored in cell_indicator_result under a 'population' indicator.
SELECT
    fcm.feature_id,
    SUM(ind.value * pop.value)
        / NULLIF(SUM(pop.value), 0)     AS population_weighted_mean
FROM cell_indicator_result ind
JOIN cell_indicator_result pop
    ON  pop.cell_id      = ind.cell_id
    AND pop.grid_id      = ind.grid_id
    AND pop.scenario_id  = :baseline_id    -- population is always baseline
    AND pop.indicator_id = :population_indicator_id
JOIN feature_cell_membership fcm
    ON  fcm.cell_id = ind.cell_id
    AND fcm.grid_id = ind.grid_id
WHERE ind.grid_id      = :ghspop_grid_id
  AND ind.scenario_id  = :scenario_id
  AND ind.indicator_id = :target_indicator_id
GROUP BY fcm.feature_id;
```

### Q5 — Full provenance trace for a feature indicator value

```sql
SELECT
    ir.feature_id,
    ir.value,
    ir.computed_at,
    mr.model_name,
    mr.model_version,
    mr.parameters,
    s.name                              AS scenario_name,
    ra.storage_uri                      AS source_raster,
    ra.checksum_sha256                  AS source_checksum
FROM indicator_result ir
JOIN model_run mr    ON mr.id = ir.run_id
JOIN scenario s      ON s.id  = ir.scenario_id
LEFT JOIN raster_asset ra ON ra.id = ANY(mr.input_asset_ids)
WHERE ir.feature_id   = :feature_id
  AND ir.indicator_id = :indicator_id
ORDER BY ir.computed_at DESC;
```

---

## 15. Performance Guidelines

### Partitioning strategy

Partition `cell_indicator_result` by `grid_id`. Create one partition per registered grid.

```sql
-- At grid registration time:
CREATE TABLE cell_indicator_result_h3r9_city_a
    PARTITION OF cell_indicator_result
    FOR VALUES IN (':grid_uuid');

-- PostgreSQL prunes irrelevant partitions at plan time,
-- not execution time. Queries with grid_id = :x never touch other partitions.
```

For deployments with many scenarios, add a sub-partition by `scenario_id`:

```sql
CREATE TABLE cell_indicator_result_h3r9_baseline
    PARTITION OF cell_indicator_result_h3r9_city_a
    FOR VALUES IN (':baseline_scenario_uuid');
```

### Index strategy

```sql
-- aoi_cell_membership: the most queried bridge table
CREATE INDEX ON aoi_cell_membership (aoi_id, grid_id);   -- AOI filter
CREATE INDEX ON aoi_cell_membership (grid_id, cell_id);  -- cell lookup

-- feature_cell_membership
CREATE INDEX ON feature_cell_membership (grid_id, cell_id);   -- cell→feature
CREATE INDEX ON feature_cell_membership (feature_id, grid_id); -- feature→cell

-- cell_indicator_result (within each partition)
-- Primary key (grid_id, cell_id, scenario_id, indicator_id) already provides
-- the main lookup index. Add a covering index for scenario comparison queries:
CREATE INDEX ON cell_indicator_result (grid_id, indicator_id, scenario_id)
    INCLUDE (cell_id, value);
```

### Cardinality limits

| Grid type | Max cells in DB | Recommended action above limit |
|---|---|---|
| H3 resolution 9 | ~500K | Partition by sub-AOI or move to GeoParquet |
| H3 resolution 10+ | Do not store in DB | Use COG/Zarr for dense outputs |
| Fishnet 100m city | ~250K–2M | Partition aggressively; consider GeoParquet |
| Fishnet 1km regional | ~50K–500K | Fine for relational DB |

### When to move to GeoParquet (analytical layer)

If any of the following apply, export `cell_indicator_result` to GeoParquet on object storage:

- Cell count × scenario count × indicator count > 50M rows
- Analytical queries take more than 2 seconds on warm cache
- Results are consumed primarily by data science workflows (Jupyter, pandas)
- Bulk exports for GIS tools are frequent

---

## 16. Anti-Patterns to Avoid

| Anti-pattern | Problem | Correct approach |
|---|---|---|
| Storing H3/fishnet geometry in the DB | Wastes storage; geometry is computable from cell_id | Compute geometry at read time from cell_id + geotransform |
| Running `ST_Intersects` at query time for AOI filtering on cells | Performance cliff at > 100K cells | Pre-compute `aoi_cell_membership` at registration time |
| Using `NULL` for baseline `scenario_id` | Breaks `IN (baseline_id, scenario_id)` comparisons; complicates indexing | Use a fixed, well-known UUID for baseline |
| Resampling a fishnet output to H3 "for consistency" | Introduces interpolation error; breaks population-weighted calculations | Keep data in its native grid; use `feature_cell_membership` as the bridge |
| One `indicator_result` table for both features and cells | Different cardinality, different access patterns, different keys | Separate tables: `indicator_result` (feature path), `cell_indicator_result` (cell path) |
| Storing model parameters as a pointer to a config file | Config file can change; parameters are not reproducible | Store the full parameter JSONB snapshot in `model_run` |
| Overwriting baseline data when a scenario is run | Loss of the baseline state; cannot compare | Always append new rows with `scenario_id`; never update |
| Storing all cells for a dense raster in the DB | Table grows to hundreds of millions of rows | Store the raster as COG; materialize only sparse or queried cells |
| Computing `feature_cell_membership` at query time | Geometry intersection at query time is O(features × cells) | Compute once at grid registration time |

---

## 17. Real-World References

### H3 tessellation in production

**Uber H3** (open source, https://h3geo.org): Uber uses H3 as the canonical spatial index for ride pricing, surge areas, and city analytics. Their key insight — later adopted broadly — is that storing H3 indices as integer keys and doing spatial joins via B-tree lookups instead of geometry intersections reduces query latency by 2–3 orders of magnitude at city scale.

**Foursquare Studio**: Uses H3 as the universal join key between point datasets, polygon datasets, and raster-derived summaries, enabling cross-dataset spatial analytics without geometry operations at query time.

### Fishnet / raster-aligned grids in production

**Microsoft Planetary Computer**: All raster data is stored as Cloud Optimized GeoTIFFs catalogued via STAC. Feature-level analytics are served as pre-aggregated zonal statistics. This is structurally identical to the raster_asset + indicator_result pattern described here.

**EU Copernicus Urban Atlas + GHS-POP**: The standard approach in European urban analytics is to align all model outputs to the GHS-POP 100m grid and compute population-weighted indicators via pixel-level joins — exactly the population-weighted query in Section 14 (Q4).

### STAC as the raster catalog standard

**STAC Specification** (stacspec.org): The SpatioTemporal Asset Catalog spec is the de facto industry standard for describing geospatial raster and vector assets. NASA, ESA, Google Earth Engine, and Microsoft Planetary Computer all publish data as STAC catalogs. The `raster_asset` table in this architecture is a relational implementation of a STAC item.

### GeoParquet as the analytical layer standard

**GeoParquet** (geoparquet.org, OGC standard v1.1 2024): The Overture Maps Foundation distributes buildings, roads, and places for the entire planet as partitioned GeoParquet on S3. This is the recommended format for bulk analytical reads and data sharing.

### exactextract for zonal statistics

**exactextract** (github.com/isciences/exactextract): The industry-standard library for computing fractional coverage zonal statistics between raster pixels and vector polygons. Used by the GDAL project and recommended over `rasterstats` for production workloads requiring accurate area-weighted aggregation.
