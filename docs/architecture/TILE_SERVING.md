# Tile Serving

Tile serving in Reimagina follows a hybrid rule:

- **live MVT** is the system of record for current, filtered, scenario-aware, or frequently changing map data
- **PMTiles** are snapshot artifacts for stable reference/context layers

## Current v1 contract

- `serving_geo.get_tile()` + **Martin** remains the primary live tile path
- PMTiles are additive, not a replacement
- v1 PMTiles target: `admin_reference` from `serving_geo.admin_v`
- PMTiles are rebuilt manually, on schedule, or on release publication
- PMTiles are **not** rebuilt on every ingestion run

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
- macOS GDAL builds often lack the SQLite dialect used by the PMTiles driver's post-write step, so `ogr2ogr` exits non-zero (`OGR2SQLITE_Setup() failed`) even when a valid artifact is written. `reimagina.tiles.service._run_export_command` detects this pattern + verifies the artifact exists on disk before swallowing the exit code, so the flow works on macOS dev boxes and CI alike. Any other non-zero exit re-raises.

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
