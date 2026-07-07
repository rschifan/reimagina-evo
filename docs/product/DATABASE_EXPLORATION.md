# Database Exploration for Reimagina

This project uses three different database layers for three different reasons:

- `curated_geo`: source-of-truth tables
- `serving_geo`: app-facing read contract
- `explore`: engineer-facing inspection views

## Recommended tools

- **DBeaver Community**: the default SQL explorer for Postgres/PostGIS
- **QGIS**: optional secondary tool for visual map and geometry checks

## Recommended starting point

When you open the database in DBeaver, start from the `explore` schema:

- `explore.layer_catalog_v`
  - what important layers exist and what they are for
- `explore.source_runs_v`
  - what datasets are registered and what ingestion runs have happened
- `explore.feature_type_stats_v`
  - what canonical features are loaded
- `explore.serving_stats_v`
  - what the serving layer actually exposes

## When to move deeper

- Go to `serving_geo` when you want to inspect what the API or workers should read.
- Go to `curated_geo` when you want to debug lineage, canonical storage, or write-path issues.
- Use QGIS when you need to inspect spatial layout, overlap behavior, or geometry quality visually.

## Standard local connections

### Postgres/PostGIS

Typical local connection:

```text
postgresql://localhost/reimagina_pg_run2
```

## CLI shortcuts

The engineer-facing CLI mirrors the `explore` views:

```bash
python -m apps.cli.app explore layers
python -m apps.cli.app explore sources
python -m apps.cli.app explore feature-stats
python -m apps.cli.app explore serving-stats
```

These commands are read-only and are intended for quick terminal inspection and scripting.
