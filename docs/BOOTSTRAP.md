# Bootstrap runbook — fresh-machine ingest

This is the operator runbook for rebuilding `reimagina_app` from
scratch. Follow it top-to-bottom on a clean machine that has Postgres
+ PostGIS reachable, the repo checked out, and `make install` already
run.

The current target state is:

- **2 AOIs:** CDMX (all), Mexicali
- **Geometry features:** INEGI 2024 (blocks, AGEBs, municipalities,
  states for entidad codes 09 + 02) + Overture buildings clipped to
  each AOI's polygon
- **Observations:** INEGI 2010 census headline columns + their
  AGEB/municipality rollups
- **Indicators:** `urban_form_block_compactness`,
  `urban_form_block_building_density` for both AOIs

## 0. Pre-flight

```sh
make install                   # Python + web deps
export DATABASE_URL=postgresql://localhost/reimagina_app
createdb reimagina_app
make migrate                   # apps.cli.app bootstrap-db
make verify-sources            # data verify-sources (uses OVERTURE_DEFAULT_RELEASE)
```

`make verify-sources` must print `"all_ok": true` before continuing —
it confirms the Azure data-lake mirror under `data/sources/`, the
Overture S3 release, and the WorldPop REST catalogue (`MEX` by
default) are all reachable. Any red entry blocks ingest: fix the path
/ network / release version before going further.

To target a different Overture release: `make verify-sources` accepts
no flags; for a non-default release run the CLI directly:
`uv run python -m apps.cli.app data verify-sources --release <X>`. If
the release exists, also bump `OVERTURE_DEFAULT_RELEASE` in
`packages/py/reimagina/pipeline/overture.py` so subsequent commands
default to it.

## 1. Feature-type catalog

```sh
uv run python -m apps.cli.app feat seed-types
```

Idempotent. ~1 s.

## 2. Geometry ingest (INEGI 2024)

```sh
uv run python -m apps.cli.app inegi-marco bootstrap \
  --cve-ent 09,02 \
  --layers areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas
```

Loads ~143 k features (states + munis + AGEBs + blocks for entidad 09
and 02). ~5–8 min depending on disk I/O.

## 3. Create AOIs

```sh
uv run python -m apps.cli.app cdmx aoi-from-state --cve-ent 09 --name "CDMX (all)"
uv run python -m apps.cli.app cdmx aoi-from-state --cve-ent 02 --name "Mexicali"
uv run python -m apps.cli.app aoi list   # confirm both rows + capture uuids
```

Save the two uuids — subsequent steps reference them via `--aoi-id`.

## 4. Spatial overlap pairs

```sh
uv run python -m apps.cli.app overlap declare-pair \
  --source-context MEX --source-slug block \
  --target-context MEX --target-slug ageb
uv run python -m apps.cli.app overlap declare-pair \
  --source-context MEX --source-slug block \
  --target-context MEX --target-slug municipality
uv run python -m apps.cli.app overlap compute-all
```

~1 min. Required before census rollups (step 7) and `aoi_feature`
recompute (step 6).

## 5. Overture buildings — per AOI

The Overture S3 query is rectangular, so each ingest is bbox-driven
plus a polygon clip on load. Bbox values come from `aoi list` (fields
`bbox` = `[xmin, ymin, xmax, ymax]`).

```sh
# CDMX (all) — bbox roughly -99.36,19.05,-98.94,19.59
# The `=` form is no longer required for a negative xmin: the CLI is
# typer/click since #30, and click parses `--bbox -99.36,…` correctly.
# The `=` form still works, so existing scripts need no change.
#
# --release is omitted deliberately. Overture keeps only its most recent
# releases and deletes the rest, so any pin written here has a shelf life of
# weeks — three already rotted (2026-02-18.0 was never published, 2026-04-15.0
# and 2026-07-22.0 were published then removed). The default lives in one
# place, OVERTURE_DEFAULT_RELEASE (2026-10-21.0 as of 2026-11-10), and a
# stale one fails with a message naming what S3 actually has rather than a
# bare FileNotFoundError.
uv run python -m apps.cli.app overture extract \
  --bbox=-99.36,19.05,-98.94,19.59 \
  --output-path data/overture_buildings_cdmx.parquet
uv run python -m apps.cli.app overture load \
  --raw-path data/overture_buildings_cdmx.parquet \
  --source-name "Overture Buildings - CDMX (all)" \
  --aoi-id <CDMX_AOI_UUID>

# Mexicali — the default --bbox is MEXICALI_BBOX, the urban core.
#
# ⚠ Not the whole municipio. Municipio de Mexicali is 9,971 km2 and reaches
# the Sea of Cortez at San Felipe; MEXICALI_BBOX is the ~0.5x0.4 degree
# urban box, which is what holds the city. Pass --bbox explicitly for the
# full municipal extent, and expect the extract to cost accordingly.
uv run python -m apps.cli.app overture extract \
  --output-path data/overture_buildings_mexicali.parquet
uv run python -m apps.cli.app overture load \
  --raw-path data/overture_buildings_mexicali.parquet \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id <MEXICALI_AOI_UUID>
```

`--aoi-id` triggers a post-load `ST_Intersects` clip — buildings inside
the bbox but outside the AOI polygon are dropped before any further
pipeline work touches them.

**The whole sequence as one command.** Everything in this document for
Mexicali — schema, INEGI geometry, census, amenities, Overture buildings, the
OSM network, membership, overlaps, indicators — is `scripts/mexicali_run.sh`.
It checks the mirror first and names each missing path, keeps the step order
that matters (membership *after* every load, or every tile returns 204), and
treats the Overpass fetch as non-fatal so a rate-limit does not discard the
INEGI load. Use the steps below to run a part of it, or to understand what it
does.

```sh
export DATABASE_URL=postgresql://localhost/reimagina_mexicali
export DATABASE_URL_ADMIN=postgresql://localhost/postgres
scripts/mexicali_run.sh
```

**Where `<MEXICALI_AOI_UUID>` comes from without the lake mirror.** Every
other AOI constructor is INEGI-derived (`cdmx aoi-from-alcaldia` needs a
`MEX:municipality` feature loaded from Marco Geoestadistico). Overture's
`divisions` theme carries the same boundaries, in the release the buildings
already come from:

```sh
# What this release actually calls things, so --name is checkable
uv run python -m apps.cli.app overture divisions

uv run python -m apps.cli.app overture aoi-from-division \
  --aoi-name "Mexicali (Overture municipal boundary)" \
  --subtype county --country MX --name "Municipio de Mexicali"
```

⚠ **Always pass `--country` for a border-crossing bbox.** The Mexicali box
holds both `MX Municipio de Mexicali` and `US Imperial County`, and both are
`subtype=county`. Selecting the wrong one produces a valid polygon, a
successful load and a map of the wrong country, with no error anywhere.

Measured on this bbox against release 2026-10-21.0 (2026-11-10):

| step | result |
|---|---|
| `overture divisions` | 16 divisions, 2 countries |
| `overture aoi-from-division` | valid MultiPolygon, 2,109 points, 9,971 km2 |
| `overture extract` | 387,273 rows, 51.2 MB, 40 s, **peak RSS 4.4 GB** |
| `overture load` | 387,273 written, **43,577 clipped** outside the Mexican boundary |
| loaded buildings | **343,696**, extent capped at the border (ymax 32.676, from a bbox ymax of 32.80) |

⚠ The 4.4 GB peak is the open risk `_extract_remote_overture_subset`
already documents — it materialises the whole bbox subset in memory
(`pq.write_table(dataset.to_table(...))`), about 11.5 KB of RAM per row. One
urban bbox fits in a 15 GB container with room to spare; the full municipio,
or a metro the size of CDMX, may not. The remedy is written beside the code:
`dataset.to_batches()` + `ParquetWriter`. Measure before assuming you need
it.

## 5b. WorldPop population raster — per AOI (optional)

The WorldPop public REST API publishes per-country population rasters
(`wpgp` = per-pixel population, 100 m). The CLI fetches the country
GeoTIFF (cached so re-runs against multiple AOIs only download once)
and clips it to the AOI polygon before handing the local file to the
existing `raster ingest` step. No FTP client or manual download
script needed.

```sh
# Step 1 — fetch + clip to CDMX. Country raster lands in
# data/cache/worldpop/wpgp_MEX_2020.tif (~1 GB, cached).
uv run python -m apps.cli.app raster fetch-worldpop \
  --country MEX --year 2020 \
  --aoi-id <CDMX_AOI_UUID> \
  --output-path data/worldpop_cdmx_2020_100m.tif

# Step 2 — register + COG-encode through the existing pipeline.
uv run python -m apps.cli.app raster ingest \
  --raw-path data/worldpop_cdmx_2020_100m.tif \
  --layer-slug worldpop_cdmx_2020_100m \
  --aggregation-kind extensive \
  --source-name "WorldPop · MEX · 2020 (100m, CDMX clip)" \
  --provider WorldPop --version 2020-100m

# Repeat both steps for Mexicali — the cached country file is reused.
uv run python -m apps.cli.app raster fetch-worldpop \
  --country MEX --year 2020 \
  --aoi-id <MEXICALI_AOI_UUID> \
  --output-path data/worldpop_mexicali_2020_100m.tif
uv run python -m apps.cli.app raster ingest \
  --raw-path data/worldpop_mexicali_2020_100m.tif \
  --layer-slug worldpop_mexicali_2020_100m \
  --aggregation-kind extensive \
  --source-name "WorldPop · MEX · 2020 (100m, Mexicali clip)" \
  --provider WorldPop --version 2020-100m
```

First fetch: ~5–10 min (downloading the country raster from
`ftp.worldpop.org.uk`). Subsequent fetches against new AOIs reuse the
cache and complete in seconds. Each `raster ingest` is ~30 s for an
AOI-sized raster.

## 6. AOI feature overlap

```sh
uv run python -m apps.cli.app aoi compute-features
```

Runs every AOI by default. Pass `--aoi-id <UUID>` to recompute one
without rebuilding the whole matrix. Idempotent — existing rows for
each target AOI are deleted first.

This populates `aoi_feature` against the now-loaded buildings + INEGI
geometries. ~3 min per AOI.

## 7. INEGI 2010 census

```sh
uv run python -m apps.cli.app inegi-censo bootstrap --cve-ent 09,02
```

~5 min. Block-grain census attributes land in `curated_geo.observation`.

Rollups (block → AGEB / municipality) are intentionally not a CLI
subcommand — they're operator-driven through the generic aggregation
recipe machinery (see `apps/cli/inegi.py` and `raster derivation` /
`aggregation-recipe` commands). Wire the recipes you actually need,
when you need them.

## 8. Indicators

```sh
uv run python -m apps.cli.app indicators seed-catalog

for AOI in <CDMX_AOI_UUID> <MEXICALI_AOI_UUID>; do
  uv run python -m apps.cli.app indicators compute \
    --slug urban_form_block_compactness --aoi-id $AOI
  uv run python -m apps.cli.app indicators compute \
    --slug urban_form_block_building_density --aoi-id $AOI
done
```

~5 min per (slug, AOI) pair.

## 9. Integrity probe

Run these three queries — all three must return the expected shape:

```sql
-- (a) every feature reaches at least one AOI (no orphans)
SELECT count(*) FROM curated_geo.feature f
WHERE NOT EXISTS (SELECT 1 FROM curated_geo.aoi_feature af WHERE af.feature_id=f.id);
-- expect: 0

-- (b) no duplicate features within (type, source_record_ref, dataset_version)
SELECT sum(c-1) FROM (
  SELECT type_slug, source_record_ref, dataset_version, count(*) c
  FROM curated_geo.feature GROUP BY 1,2,3 HAVING count(*)>1) t;
-- expect: NULL or 0

-- (c) row counts by type look right
SELECT type_slug, count(*) FROM curated_geo.feature GROUP BY type_slug;
-- expect: building ~2M, block ~85k, ageb ~2.9k, municipality ~18,
-- state 2 (numbers depend on the exact entidades + Overture release)
```

## 10. Smoke services

```sh
make dev &          # API on :8001 and web on :5173 together
martin --config infra/docker/martin.yaml &   # Martin on :3000 (optional)
```

In the browser hit `/aois` and toggle each layer at zoom 11–14 — the
buildings, blocks, AGEBs, and municipality choropleths should all
render without empty tiles.

## Total wall-clock

~1 hour on a modern laptop with a warm `data/sources/` lake mirror.
Add another 15–30 min if the lake mirror needs to be re-downloaded
from Azure.

## Rolling forward to a new Overture release

When Overture publishes a new release (monthly), update one constant:

```python
# packages/py/reimagina/pipeline/overture.py
OVERTURE_DEFAULT_RELEASE = "2026-MM-DD.0"
```

To find out what you can pin to, ask S3 rather than guessing — this is
also what the `FileNotFoundError` message prints for you when a pin has
rotted:

```sh
uv run python -c "from reimagina.pipeline.overture import \
available_overture_releases as r; print(r())"
```

Then `make verify-sources` will probe the new release. The previously
loaded buildings keep their old `dataset_version` tag; the new ingest
goes in alongside (or replaces, if you re-run step 5 with the same
`--source-name`).
