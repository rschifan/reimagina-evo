# Tile Serving

Tile serving in Reimagina follows a hybrid rule:

- **live MVT** is the system of record for current, filtered, scenario-aware, or frequently changing map data
- **PMTiles** are snapshot artifacts for stable reference/context layers

## Current v1 contract

- `serving_geo.get_tile()` + **Martin** remains the primary live tile path
- `serving_geo.indicator_overlay_tile()` adds a live choropleth channel
  (details below), also auto-published by Martin.
- PMTiles are additive, not a replacement
- v1 PMTiles target: `admin_reference` from `serving_geo.admin_v`
- PMTiles are rebuilt manually, on schedule, or on release publication
- PMTiles are **not** rebuilt on every ingestion run

### `serving_geo.get_tile(z, x, y, query_params json)`

The baseline feature channel. Martin publishes it at
`GET /get_tile/{z}/{x}/{y}?…`. Query params: `type_slug`, `role`, `aoi_id`
— all optional.

- **`aoi_id` scopes to that AOI's members** via `EXISTS` against
  `curated_geo.aoi_feature` (r039). `EXISTS` rather than a join, because a
  feature can belong to several AOIs and a join would emit it once per
  match and duplicate it in the tile.
  - Omitting it stays **unscoped** — a CDMX-wide view is a legitimate ask,
    and the parameter narrows rather than enables.
  - `?aoi_id=` (present but blank) reads as unscoped; an empty string is
    how a client serialises "no AOI selected".
  - A malformed uuid renders an **empty tile, not an error**. A tile
    function returns `bytea` and has no error channel, and empty is the
    fail-closed direction — before r039 an unrecognised param was silently
    dropped and showed *everything*.
- **Buildings are served only from `z >= 14`** (r038). Below that the
  request returns 204. This is not a preference:

  | zoom | candidates in tile | MVT bytes |
  |---|---|---|
  | 11 | 242,317 | 1.31 MB |
  | 12 | 35,033 | **2.59 MB** |
  | 14 | 421 | 31 KB |

  Against a renderer budget of ~500 KB with several tiles in flight, the
  layer never finished painting — presenting as *missing data* rather than
  as slowness. Two cheaper fixes were measured and rejected: dropping
  attributes saved 362 bytes of 2.59 MB (`ST_AsMVT` dictionary-encodes
  properties; the 1.71 MB hog is the per-feature uuid `id`), and
  simplifying harder kept only 1,826 of 37,858 footprints. At z12 one pixel
  is ~36 m and a building is ~10 m, so there is no encoding that helps.
  **For building information at AOI scale use the
  `urban_form_block_building_density` choropleth**, which is the layer that
  question actually wants.

  ⚠ The gate keys on `type_slug`, not `role`: buildings are
  `role='context'`, the same role as `municipality`, so any role-based rule
  that drops buildings at overview zoom also drops the municipality
  outlines.

### `serving_geo.indicator_overlay_tile(z, x, y, query_params json)`

- Martin auto-discovery publishes the function at
  `GET /indicator_overlay_tile/{z}/{x}/{y}?…`.
- Required query param: `indicator_slug` — the function returns an
  empty tile when absent so a misconfigured caller never leaks the
  whole table.
- Optional query param: `scenario_id` (uuid). Omit for base state.
- Returns empty below `z=10`; choropleth below city scale is misleading
  (every feature collapses into one pixel). `infra/tiles/layers.json`
  reflects this with `minzoom: 10`.
- Emits per feature: `feature_id`, `type_slug`, `value_numeric`,
  `quality_flag`. Frontend's `fill-color` expression reads
  `value_numeric`; `quality_flag` is used to dim `estimated` / `missing`
  rows if the UI wants to distinguish them.

## Source classification

The infrastructure-owned layer manifest lives at:

- `infra/tiles/layers.json`

Each declared layer is classified as:

- `live_mvt`
- `pmtiles_snapshot`

In v1:

- reference/context layers may be `pmtiles_snapshot`
- canonical layers under active ingestion stay `live_mvt`
- scenario and indicator overlays stay `live_mvt`

## Dev infrastructure

- live MVT: Martin on `http://localhost:3000`

⚠ **Martin's tile cache is disabled** (`cache_size_mb: 0` in
`infra/docker/martin.yaml`). Its 512 MB default is right for static data and
wrong here: indicators are recomputed constantly and the cache cannot see
that the rows changed. Measured — after computing an indicator, a tile
requested *before* the compute kept returning `204 Empty` while the
identical call in SQL returned bytes, and neighbouring tiles never
previously requested returned data correctly. The failure mode is the worst
available: not an error, just the pre-compute answer, indistinguishable
from "this indicator produced nothing". It caused three false diagnoses in
one day. Cost of disabling is a re-query per tile, ~230 ms on the heaviest
real block tile. Raise it for a deployment where the data is not moving
under the reader, and carry the setting if you start Martin from another
config.

- PMTiles static serving: file-backed HTTP on `http://localhost:3001`
- PMTiles export output directory: `data/tiles/pmtiles`

## CLI

```bash
python -m apps.cli.app tiles manifest
python -m apps.cli.app tiles export admin_reference
python -m apps.cli.app tiles sources
python -m apps.cli.app tiles snapshots
```

Runtime note:

- `tiles export` requires `ogr2ogr` with PostgreSQL and PMTiles driver support (GDAL ≥ 3.8)
- local sandboxed environments may block the export subprocess from connecting to Postgres over TCP even when the CLI code is correct
- macOS GDAL builds often lack the SQLite dialect used by the PMTiles driver's post-write step, so `ogr2ogr` exits non-zero (`OGR2SQLITE_Setup() failed`) even when a valid artifact is written. `reimagina.tiles.export._run_export_command` detects this pattern + verifies the artifact exists on disk before swallowing the exit code, so the flow works on macOS dev boxes and CI alike. Any other non-zero exit re-raises.

## Metadata surface

PMTiles snapshots are tracked in:

- `curated_geo.tile_snapshot_registry`

Each row records:

- `layer_id`
- `artifact_path`
- `artifact_uri`
- `snapshot_version`
- `source_relation`
- `source_lineage_ref`
- `status`
- `created_at`

## Rule of thumb

If a layer must reflect database changes immediately, it must stay on the live MVT path.
If snapshot freshness is acceptable and cheap/static distribution matters more, export it to PMTiles.
