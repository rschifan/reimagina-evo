# Bootstrap runbook — fresh-machine ingest

This is the operator runbook for rebuilding `reimagina_app` from
scratch. Follow it top-to-bottom on a clean machine that has Postgres
+ PostGIS + pgRouting reachable (r037 runs `CREATE EXTENSION
pgrouting`; `make stack-up`'s image carries both), the repo checked out,
`make install` already run, the lake mirror under `data/sources/lake/`
(INEGI geometry, census, amenities) and internet access (Overture,
OpenStreetMap, WorldPop).

The current target state is:

- **2 AOIs:** CDMX (all), Mexicali
- **Geometry features:** INEGI 2024 (blocks, AGEBs, municipalities,
  states for entidad codes 09 + 02) + Overture buildings clipped to
  each AOI's polygon
- **Observations:** INEGI 2020 census headline columns at block grain
  (AGEB/municipality rollups are optional — step 7)
- **Indicators:** `urban_form_block_compactness`,
  `urban_form_block_building_density` for both AOIs

## 0. Pre-flight

Complete the [installation guide](INSTALLATION.md) first, including the
five DSNs, database readiness, `make install` and `make migrate`. For Docker,
start PostgreSQL and PgBouncer before migrations, then start Martin. Then:

```sh
make verify-sources            # uses OVERTURE_DEFAULT_RELEASE; needs network/mirror
```

`.env` is read by every step below: the CLI loads it itself
(`load_dotenv` in `reimagina/db/connection.py`; a variable already set in the
shell wins), and `make dev` and `make dev-api` fall back to its
`DATABASE_URL`. The keys:

| Key | Read by |
|---|---|
| `DATABASE_URL` | the CLI, loaders, migrations; with Path B, `postgresql://localhost/reimagina_app` |
| `REIMAGINA_APP_DSN` | the API (`make dev` and `make dev-api` use `DATABASE_URL` when it is unset) |
| `DATABASE_URL_DIRECT` | the run worker and the ops-plane schema; required, never through pgbouncer |
| `TEST_DATABASE_URL` | `make migrate`'s test database and the integration suite |
| `DATABASE_URL_ADMIN` | the database `make migrate` connects to in order to `CREATE DATABASE` (default `postgresql://localhost/postgres`) |

`make migrate` creates the app and test databases when they are absent, so
no `createdb` is needed, then migrates both to head and provisions the
ops-plane schema.

`make verify-sources` must print `"all_ok": true` before continuing —
it confirms the private data-lake mirror under `data/sources/`, the
Overture S3 release, and the WorldPop REST catalogue (`MEX` by
default) are all reachable. Any red entry blocks ingest: fix the path
/ network / release version before going further.

To target a different Overture release: `make verify-sources` accepts
no flags; for a non-default release run the CLI directly:
`uv run python -m apps.cli.app data verify-sources --release <X>`. If
the release exists, set `REIMAGINA_OVERTURE_RELEASE=<X>` in `.env` (read
once, at import, as the default of `OVERTURE_DEFAULT_RELEASE` in
`packages/py/reimagina/pipeline/overture.py`) so subsequent commands
default to it.

## 1. Feature-type catalog

```sh
uv run python -m apps.cli.app feat seed-types
```

Idempotent. ~1 s.

## 2. Geometry ingest (INEGI 2024)

```sh
uv run python -m apps.cli.app inegi-marco bootstrap \
  --cve-ent 09 --cve-ent 02 \
  --layers areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas
```

`--cve-ent` is repeated, not comma-separated: `--cve-ent 09,02` is read as
one code, `09,02`, which matches no partition.

Loads ~143 k features (states + munis + AGEBs + blocks for entidad 09
and 02). ~5–8 min depending on disk I/O.

## 3. Create AOIs

```sh
uv run python -m apps.cli.app cdmx aoi-from-state --cve-ent 09 --name "CDMX (all)"
uv run python -m apps.cli.app cdmx aoi-from-state --cve-ent 02 --name "Mexicali"
uv run python -m apps.cli.app aoi list   # confirm both rows + capture uuids
```

Save the two uuids — subsequent steps reference them via `--aoi-id`.

⚠ `aoi-from-state --cve-ent 02` is the whole state of Baja California;
"Mexicali" is only its name. `scripts/mexicali_run.sh` bounds Mexicali by the
municipality instead (`cdmx aoi-from-alcaldia --cve-ent 02 --cve-mun 002`); with
that AOI, the rest of the state's INEGI features belong to no AOI and probe
(a) in step 9 counts them.

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

~1 min. Required before any block → AGEB / municipality rollup (step 7).
`aoi_feature` (step 6) does not read overlaps.

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

# Mexicali — pass MEXICALI_BBOX, the urban core, explicitly. The extract's
# default --bbox is MEXICO_CITY_BBOX (-99.597,18.992,-98.606,19.921), so
# leaving it out extracts CDMX under a Mexicali file name.
#
# ⚠ Not the whole municipio. Municipio de Mexicali is 9,971 km2 and reaches
# the Sea of Cortez at San Felipe; MEXICALI_BBOX is the ~0.5x0.4 degree
# urban box, which is what holds the city. Pass a wider --bbox for the
# full municipal extent, and expect the extract to cost accordingly.
uv run python -m apps.cli.app overture extract \
  --bbox=-115.70,32.40,-115.20,32.80 \
  --output-path data/overture_buildings_mexicali.parquet
uv run python -m apps.cli.app overture load \
  --raw-path data/overture_buildings_mexicali.parquet \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id <MEXICALI_AOI_UUID>
```

`--aoi-id` triggers an `ST_Intersects` clip inside the load's transaction —
buildings inside the bbox but outside the AOI polygon are dropped before the
load commits, so the published row count is net of the clip and re-running
the same load publishes nothing. A load that leaves the source with nothing
inside the AOI is refused (`ExtractAoiMismatch`) and rolled back whole, its
run marked failed: that is an extract of the wrong bbox, the default one
being Mexico City's.

**The Mexicali half as one command.** `scripts/mexicali_run.sh` builds
Mexicali alone, and more of it than this runbook: schema and catalogs, INEGI
geometry for entidad 02, an AOI on the municipality (not the state — see step
3), the 2020 census, amenities, Overture places and buildings, the OSM
network, membership, overlaps (`global:building → MEX:block`,
`MEX:block → MEX:ageb`), the scenario-input baseline (`inegi-censo bootstrap
--columns prom_ocup`, `overture block-storeys`,
`scripts/seed_demo_attributes.py --fit-to-census`) and every shipped
indicator. It checks the mirror first and names each missing path, keeps the
step order that matters (membership *after* every load, or every tile returns
204), and treats the census, amenity, places and Overpass loads as non-fatal
so one failure does not discard the INEGI load. Use the steps in this
document to run a part of it, or to understand what it does.

```sh
# ⚠ Without --keep, step 1a DROPS and recreates the DATABASE_URL database.
# Explicit exports override .env. The script validates that DATABASE_URL and
# DATABASE_URL_DIRECT identify the same direct host, port and database before
# dropping anything; DATABASE_URL_ADMIN must use that server and a different
# maintenance database (default: postgres).
scripts/mexicali_run.sh                 # --keep, --skip-network, --low-memory
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

⚠ The 4.4 GB peak is the default extract path, and it is not the rows —
those cost ~400 MB. It is parquet metadata, read while the bbox predicate is
evaluated against row-group statistics across all 512 files of the release
(`extract_overture_buildings_subset` in `pipeline/overture.py`). `overture
extract --low-memory` scans one S3 file at a time and streams batches through
a `ParquetWriter`: 663 MB peak and 454 s instead of 40 s, measured on the same
bbox and release, and a flat ceiling rather than a proportional one. Use it on
a laptop also running Postgres, where 4.4 GB does not fail cleanly but swaps
and looks hung, and for an AOI materially larger than one city;
`scripts/mexicali_run.sh --low-memory` passes it through.

## 5b. WorldPop population raster — per AOI (optional)

The WorldPop public REST API publishes per-country population rasters
(`wpgp` = per-pixel population, 100 m). The CLI fetches the country
GeoTIFF (cached so re-runs against multiple AOIs only download once)
and clips it to the AOI polygon before handing the local file to the
existing `raster ingest` step. No FTP client or manual download
script needed. `fetch-worldpop` reads the AOI polygon through `--dsn`, which
defaults to `DATABASE_URL` (from `.env` too) like every other DSN option.

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

First fetch: ~5–10 min (downloading the country raster — ~893 MB for
Mexico — from the GeoTIFF URL WorldPop's REST catalogue lists for it).
Subsequent fetches against new AOIs reuse the cache and complete in
seconds. Each `raster ingest` is ~30 s for an AOI-sized raster.

## 6. AOI feature overlap

```sh
uv run python -m apps.cli.app aoi compute-features
```

Runs every AOI by default. Pass `--aoi-id <UUID>` to recompute one
without rebuilding the whole matrix. Idempotent — existing rows for
each target AOI are deleted first.

This populates `aoi_feature` against the now-loaded buildings + INEGI
geometries. ~3 min per AOI.

## 7. INEGI 2020 census

```sh
uv run python -m apps.cli.app inegi-censo bootstrap --cve-ent 09 --cve-ent 02
```

~5 min. Block-grain census attributes land in `curated_geo.observation`:
by default the headline subset `pobtot`, `pobmas`, `pobfem`, `p_15ymas`,
`p_18ymas`, `vivtot`; `--columns` names others (`--columns prom_ocup` lands
household size as `people_per_household`, the scenario input).

Rollups (block → AGEB / municipality) are intentionally not a CLI
subcommand — they're operator-driven through the generic aggregation
recipe machinery (see `apps/cli/inegi.py` and the `raster derivation`
commands: `add`, `list`, `enable`, `run`, `remove`). Wire the recipes you
actually need, when you need them.

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
make dev            # API on :8001, web on :5173 and the run worker together
```

`make dev` serves the tiles from the API itself (`/api/tiles`), so Martin
is not needed. To use Martin from the container stack instead (Path A),
start it as `REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000 make dev`.
`make dev-api`, `make dev-web` and `make dev-worker` start the three
processes one at a time; `make dev-api` resolves the DSN and the tile URL
the same way `make dev` does.

`curl -s http://127.0.0.1:8001/aois` should list both AOIs. Open
`http://localhost:5173`, pick each area under *Area*, and turn on each
layer under *Layers* at zoom 11–14 — the buildings, blocks and
municipalities should all render without empty tiles, and the indicator
choropleth should colour the blocks.

## Total wall-clock

~1 hour on a modern laptop with a warm `data/sources/` lake mirror.
Add another 15–30 min if the lake mirror needs to be re-downloaded
from private.

## Rolling forward to a new Overture release

When Overture publishes a new release (monthly), set one variable in
`.env` (the built-in default is `OVERTURE_DEFAULT_RELEASE` in
`packages/py/reimagina/pipeline/overture.py`):

```sh
REIMAGINA_OVERTURE_RELEASE=2026-MM-DD.0
```

or, for one shell without a code change, `export
REIMAGINA_OVERTURE_RELEASE=2026-MM-DD.0`.

To find out what you can pin to, ask S3 rather than guessing — this is
also what the `FileNotFoundError` message prints for you when a pin has
rotted:

```sh
uv run python -c "from reimagina.pipeline.overture import \
available_overture_releases as r; print(r())"
```

Then `make verify-sources` will probe the new release. The previously
loaded buildings keep their old `dataset_version` tag, and the new ingest
goes in alongside them whatever `--source-name` you pass: features are
unique on `(source_id, dataset_version, source_record_ref)`, and nothing in
step 5 deletes an earlier release's rows.
