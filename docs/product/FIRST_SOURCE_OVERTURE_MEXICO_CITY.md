# First Source Example: Overture Buildings for Mexico City

This document makes the first example source concrete for the current Reimagina system.

## Chosen source

- **Provider**: Overture Maps Foundation
- **Theme / type**: `buildings / building`
- **Purpose in the system**: first canonical vector source loaded into `curated_geo.feature`
- **Storage type**: `vector`
- **Integration role**: `canonical_entity`
- **Feature type**: `global / building`
- **Feature role**: `context`

## Bounding box

Use the broader Mexico City bbox:

```text
min_lon = -99.597
min_lat = 18.992
max_lon = -98.606
max_lat = 19.921
```

CLI form:

```text
-99.597,18.992,-98.606,19.921
```

## Suggested first workflow

1. Bootstrap the local app and test databases:

```bash
python -m apps.cli.app bootstrap-db
```

2. Verify the canonical first-layer relations exist:

```bash
python -m apps.cli.app verify-schema
```

3. Print the pinned DuckDB extraction SQL:

```bash
python -m apps.cli.app overture print-extract-sql
```

4. Extract only the Mexico City subset to local GeoParquet with DuckDB:

```bash
python -m apps.cli.app overture extract \
  --output-path data/overture_buildings_mexico_city.parquet
```

5. Register, stage, and load the subset:

```bash
python -m apps.cli.app overture load --raw-path data/overture_buildings_mexico_city.parquet
```

6. Validate the serving layer:

```bash
python -m apps.cli.app overture validate
```

## Canonical mapping

- `source_record_ref` <- Overture `id`
- `geometry` <- Overture geometry
- `attributes` <- selected stable properties only

Initial retained attribute keys:

- `subtype`
- `class`
- `height`
- `num_floors`
- `names`
- `sources`

## Python helpers in the repo

The current implementation provides reusable helpers in:

- `reimagina.pipeline.overture`
- `apps.cli.overture`

Key functions:

- `overture_buildings_extract_sql()`
- `extract_overture_buildings_subset(...)`
- `bootstrap_overture_buildings_subset(...)`
- `register_overture_buildings_source(...)`
- `stage_overture_buildings_subset(...)`
- `load_overture_buildings_subset(...)`
- `validate_overture_buildings_layer(...)`

Developer CLI entrypoints:

- `python -m apps.cli.app bootstrap-db`
- `python -m apps.cli.app verify-schema`
- `python -m apps.cli.app overture example`
- `python -m apps.cli.app overture print-extract-sql`
- `python -m apps.cli.app overture extract --output-path ...`
- `python -m apps.cli.app overture load --raw-path ...`
- `python -m apps.cli.app overture validate`

## Notes

- Start with `buildings` because it is cleaner than transportation for the first ingestion example.
- Keep the release pinned in `version` so the first source is reproducible.
- GeoParquet is now the preferred Bronze/Silver format for this source.
