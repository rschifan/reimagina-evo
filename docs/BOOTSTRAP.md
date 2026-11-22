# Bootstrap runbook — the Mexicali territory

This is the reference for the Mexicali source bundle and the step-by-step form
of `scripts/mexicali_run.sh`. To load Mexicali in one command, follow
[installation step 9](INSTALLATION.md#9-load-the-real-mexicali-territory);
come here to run a part of the load, to understand what each step does, to
load the bundle offline, or to rebuild the bundle.

What `scripts/mexicali_run.sh` builds, into the database `DATABASE_URL` names:

- **1 AOI:** "Mexicali", bounded by the INEGI municipality (entidad 02,
  municipio 002), in project "Mexicali Pilot"
- **Features:** INEGI Marco Geoestadístico 2024 (state, municipalities, urban
  AGEBs and blocks) for the whole state of Baja California; INEGI DENUE
  amenities; Overture places and buildings; the OSM walking network,
  published as roads
- **Observations:** the INEGI 2020 census headline columns at block grain,
  household size, and the scenario-input baseline
- **Indicators:** all 12, over the AOI's 19,058 blocks
- No scenarios and no orchestration runs

## The Mexicali source bundle

One directory (77 MB) with every input the Mexicali load reads:

| Path in the bundle | Content |
|---|---|
| `inegi/marco_geoestadistico_2024/<layer>.parquet/CVE_ENT=02/CVE_MUN=…/` | INEGI Marco Geoestadístico 2024: state, municipalities, urban AGEBs, urban blocks of Baja California |
| `inegi/censo_2020/manzanas_2020.parquet/entidad=2/` | INEGI Censo de Población y Vivienda 2020 by block, Baja California, the columns the loader reads |
| `inegi/denue/equipamientos_mexicali.parquet` | INEGI DENUE selection for Mexicali (health, pharmacy, childcare, cinema), May 2025 |
| `overture/buildings_<release>.parquet`, `overture/places_<release>.parquet` | Overture extracts of the Mexicali box (places: Mexico only) |
| `osm/walk_network_<date>/{nodes,edges}.parquet` | the OSM pedestrian graph `network bootstrap` fetched on that date |
| `ATTRIBUTION.md` | credits and licence terms: INEGI free-use terms with the transformation notice, ODbL, CDLA-Permissive-2.0 |
| `MANIFEST.json`, `SHA256SUMS` | every file with rows, sha256, source, vintage, licence and how it was produced |

It is published as a GitHub Release asset of `rschifan/reimagina-evo`, tag
`mexicali-bundle-2026.11`, asset `reimagina-mexicali-bundle-2026.11.tar.gz`
(65 MB, SHA-256
`58e03f78f683065f3e5154eb3fde0d8b336b2df814796ad736b887e8eada4d3b`), next to
its `.sha256` and `ATTRIBUTION.md`:
<https://github.com/rschifan/reimagina-evo/releases/tag/mexicali-bundle-2026.11>.
Its version, URL, SHA-256 and top-level directory (`reimagina-mexicali-bundle/`)
are pinned in `scripts/build_mexicali_bundle.py`.

**Fetching it.** `scripts/mexicali_run.sh` without `--bundle` fetches it
itself. On its own:

```sh
export BUNDLE="$(uv run python scripts/build_mexicali_bundle.py fetch)"
```

`fetch` downloads the archive into `data/bundles/2026.11/` (under
`REIMAGINA_DATA_DIR` when that is set), refuses it unless
its SHA-256 matches the pin, extracts it, runs `verify` and prints the bundle
directory, `data/bundles/2026.11/reimagina-mexicali-bundle`. A cached archive
or an extracted bundle that still verifies is reused with no network access.
`fetch --url URL` (or `MEXICALI_BUNDLE_URL`) reads from a mirror; the checksum
cannot be overridden.

**Offline, or when GitHub is unreachable**, download the asset by hand on
another machine, then check and extract it:

```sh
shasum -a 256 reimagina-mexicali-bundle-2026.11.tar.gz   # must print the SHA-256 above
tar -xzf reimagina-mexicali-bundle-2026.11.tar.gz
export BUNDLE="$PWD/reimagina-mexicali-bundle"
uv run python scripts/build_mexicali_bundle.py verify "$BUNDLE"
scripts/mexicali_run.sh --bundle "$BUNDLE"
```

(`sha256sum` on Linux.) A published archive is never replaced: new data gets
a new release tag (`mexicali-bundle-<version>`) and a new pin in the
repository.

**Rebuilding it.** The INEGI inputs are INEGI's public downloads
(inegi.org.mx: Marco Geoestadístico, Censo de Población y Vivienda 2020
results by AGEB and urban block, DENUE), converted to hive-partitioned
parquet: `<layer>.parquet/CVE_ENT=NN/CVE_MUN=NNN/*.parquet` with a WKB
`geometry` column for the Marco Geoestadístico, `entidad=N/**/*.parquet` with
INEGI's lower-case column names for the census. The conversion from INEGI's
shapefile and CSV downloads is not scripted. The rest is made with the CLI
(see [Fresh data instead of the bundle](#fresh-data-instead-of-the-bundle)):
`overture extract --bbox=-115.70,32.40,-115.20,32.80`, `overture places
--country MX --aoi-id …` (its `--output-path` keeps the extract), and
`network bootstrap --aoi-id … --clip-bbox=-115.70,32.40,-115.20,32.80
--save-graph DIR`. Then:

```sh
uv run python scripts/build_mexicali_bundle.py build --out NEW_DIR \
  --inegi-marco <marco parquet dir> --inegi-censo <census parquet dir> \
  --denue-amenities <DENUE amenity GeoParquet> \
  --overture-buildings <extract> --overture-places <extract> \
  --overture-release 2026-10-21.0 --osm-network <--save-graph DIR>
```

It keeps Baja California (`--cve-ent 02`) and the loader columns, copies the
rest unchanged, and writes `MANIFEST.json`, `ATTRIBUTION.md` and
`SHA256SUMS`; the same inputs give the same bytes.

## 0. Pre-flight

Complete [installation steps 1–5](INSTALLATION.md) first: the five DSNs,
database readiness, `make install` and `make migrate`. Then fetch the bundle
as above, so `$BUNDLE` names it.

`.env` is read by every step below: the CLI loads it itself
(`load_dotenv` in `reimagina/db/connection.py`; a variable already set in the
shell wins), and `make dev` and `make dev-api` fall back to its
`DATABASE_URL`. The keys:

| Key | Read by |
|---|---|
| `DATABASE_URL` | the CLI, loaders, migrations, and the database `scripts/mexicali_run.sh` drops and rebuilds; `reimagina` in both `.env.example` paths |
| `REIMAGINA_APP_DSN` | the API (`make dev` and `make dev-api` use `DATABASE_URL` when it is unset) |
| `DATABASE_URL_DIRECT` | the run worker and the ops-plane schema; required, never through PgBouncer; the same database as `DATABASE_URL` |
| `TEST_DATABASE_URL` | `make migrate`'s test database and the integration suite |
| `DATABASE_URL_ADMIN` | the database `make migrate` and the drop in step 1 connect to in order to `CREATE DATABASE` (default `postgres` on the same server) |

Loading from the bundle needs no network access. `make verify-sources` (it
must print `"all_ok": true`) checks the two remote sources used only when
fetching fresh data: the Overture S3 release (`OVERTURE_DEFAULT_RELEASE`) and
the WorldPop REST catalogue (`MEX`).

The steps carry the numbers of `scripts/mexicali_run.sh`'s steps, run in the
same order. Commands that need the AOI read it from `$AOI_ID` (step 3).

## 1. Schema and catalogs

```sh
uv run python -m apps.cli.app bootstrap-db
uv run python -m apps.cli.app verify-schema
uv run python -m apps.cli.app feat seed-types
uv run python -m apps.cli.app indicators seed-catalog
```

Idempotent; `make migrate` has already done the first. The script first drops
and recreates the database (step 1a, skipped with `--keep`): a synthetic
acceptance territory left in it sits at real Mexicali coordinates and would
join the real AOI.

## 2. INEGI geometry

```sh
uv run python -m apps.cli.app inegi-marco bootstrap \
  --source-root "$BUNDLE/inegi/marco_geoestadistico_2024" \
  --cve-ent 02 \
  --layers areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas
```

Loads 70,816 features: 1 state, 7 municipalities, 1,824 urban AGEBs and
68,984 urban blocks. `--source-root` has no default. `--cve-ent` is repeatable, not
comma-separated (`--cve-ent 09,02` is read as one code that matches no
partition); the bundle holds entidad 02 only.

## 3. The AOI

```sh
uv run python -m apps.cli.app cdmx aoi-from-alcaldia \
  --cve-ent 02 --cve-mun 002 --name "Mexicali" --project-name "Mexicali Pilot"
export AOI_ID=<the printed aoi_id>
```

The AOI is the INEGI municipality polygon, and membership is by INEGI
identifier: a block belongs to Mexicali because its `CVE_ENT`/`CVE_MUN` say
so. ⚠ `cdmx aoi-from-state --cve-ent 02` would be the whole state of Baja
California. The rest of the state's INEGI features belong to no AOI.

## 4. The 2020 census

```sh
uv run python -m apps.cli.app inegi-censo bootstrap \
  --source-root "$BUNDLE/inegi/censo_2020/manzanas_2020.parquet" --cve-ent 02
```

Block-grain attributes land in `curated_geo.observation`: by default the
headline subset `pobtot`, `pobmas`, `pobfem`, `p_15ymas`, `p_18ymas`, `vivtot`;
`--columns` names others (step 9b).

## 5. Amenities (INEGI DENUE)

```sh
uv run python -m apps.cli.app points bootstrap \
  --source-root "$BUNDLE" --layers mexicali_amenities
```

## 5b. Park and cultural destinations (Overture places)

```sh
uv run python -m apps.cli.app overture places \
  --raw-path "$BUNDLE/overture/places_2026-10-21.0.parquet" \
  --release 2026-10-21.0 --aoi-id "$AOI_ID"
```

⚠ Without this step the park indicator is not a measurement: DENUE carries no
park-like category, so every block would report the 1,500 m cutoff. The
extract was cut with `--country MX`; `--release` names the release it was cut
from (it is in the file name).

## 6. Overture buildings

```sh
uv run python -m apps.cli.app overture load \
  --raw-path "$BUNDLE/overture/buildings_2026-10-21.0.parquet" \
  --release 2026-10-21.0 \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id "$AOI_ID"
```

`--aoi-id` clips inside the load's transaction: buildings inside the box but
outside the AOI polygon are dropped before the load commits, so re-running the
same load publishes nothing. A load that leaves nothing inside the AOI is
refused (`ExtractAoiMismatch`) and rolled back whole: that is an extract of
the wrong box.

## 7. The OSM walking network

```sh
uv run python -m apps.cli.app network bootstrap --aoi-id "$AOI_ID" \
  --graph-dir "$BUNDLE/osm/walk_network_2026-11-10"
```

Loads the bundle's snapshot into `road_node`/`road_edge` and publishes the
edges as `global:road` features. Without `--graph-dir` it fetches from
Overpass instead.

## 8. AOI membership — after every load

```sh
uv run python -m apps.cli.app aoi compute-features --aoi-id "$AOI_ID"
```

⚠ Order is load-bearing and fails silently: tiles scope by AOI through
`aoi_feature`, so a feature loaded *after* this pass is present but every tile
of it returns 204. Re-run it after any later load. Idempotent.

## 9. Overlaps

```sh
uv run python -m apps.cli.app overlap declare-pair \
  --source-context global --source-slug building \
  --target-context MEX --target-slug block
uv run python -m apps.cli.app overlap declare-pair \
  --source-context MEX --source-slug block \
  --target-context MEX --target-slug ageb
uv run python -m apps.cli.app overlap compute-all --aoi-id "$AOI_ID"
```

Needed by observation rollups (block → AGEB). For municipal rollups declare
`MEX:block → MEX:municipality` the same way. Rollups are not a CLI subcommand:
they run through the aggregation recipes (`raster derivation add`, `list`,
`enable`, `run`, `remove`).

## 9b. The scenario-input baseline

```sh
uv run python -m apps.cli.app inegi-censo bootstrap \
  --source-root "$BUNDLE/inegi/censo_2020/manzanas_2020.parquet" \
  --cve-ent 02 --columns prom_ocup
uv run python -m apps.cli.app overture block-storeys --aoi-id "$AOI_ID"
uv run python scripts/seed_demo_attributes.py --aoi-id "$AOI_ID" --fit-to-census
```

Household size is INEGI's `prom_ocup` (landed as `people_per_household`),
storeys are the median Overture `num_floors` per block where buildings are
tagged (written as `floors`), and the rest is synthetic and fitted to the
census (`scripts/seed_demo_attributes.py` says how). Without this step
`planned_floors`, the edificability family and `livability_block_score` write
no rows.

## 10. Indicators

```sh
# Dependency order: a composite reads its inputs' current rows.
for SLUG in urban_form_block_compactness urban_form_block_building_density \
    urban_form_block_composite urban_form_block_planned_floors \
    edificability_block_housing_units edificability_block_population \
    edificability_block_density accessibility_block_school_network_m \
    accessibility_block_health_network_m accessibility_block_park_network_m \
    accessibility_block_culture_network_m livability_block_score; do
  uv run python -m apps.cli.app indicators compute --slug "$SLUG" --aoi-id "$AOI_ID"
done
```

Each prints `rows_written`; on Mexicali every one writes 19,058, one per
block.

## 11. Inspection

```sh
uv run python -m apps.cli.app explore feature-stats
uv run python -m apps.cli.app explore serving-stats
uv run python -m apps.cli.app explore sources
```

The script stops here. Integrity probes, in `psql` against `DATABASE_URL`:

```sql
-- (a) feature counts by type
SELECT type_slug, count(*) FROM curated_geo.feature GROUP BY 1 ORDER BY 1;
-- expect, from the 2026.11 bundle: ageb 1,824 · block 68,984 ·
-- building 343,707 · establishment 2,612 · municipality 7 · road 117,858 ·
-- state 1. The blocks outside the municipality (49,926 of them) belong to no
-- AOI.

-- (b) no duplicate features within (type, source_record_ref, dataset_version)
SELECT sum(c-1) FROM (
  SELECT type_slug, source_record_ref, dataset_version, count(*) c
  FROM curated_geo.feature GROUP BY 1,2,3 HAVING count(*)>1) t;
-- expect: NULL

-- (c) every indicator has one current row per block of the AOI
SELECT indicator_slug, count(*) FROM curated_geo.indicator_result
WHERE status = 'current' AND scenario_id IS NULL GROUP BY 1 ORDER BY 1;
-- expect: 12 rows, each 19,058; and no scenarios or orchestration runs:
SELECT (SELECT count(*) FROM app_core.scenario),
       (SELECT count(*) FROM jobs_runtime.orchestration_run);
-- expect: 0 | 0
```

## 12. Smoke services

```sh
make dev            # API on :8001, web on :5173 and the run worker together
```

`make dev` serves the tiles from the API itself (`/api/tiles`), so Martin is
not needed. To use Martin from the container stack instead (Path A), start it
as `REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000 make dev`.
`make dev-api`, `make dev-web` and `make dev-worker` start the three
processes one at a time; `make dev-api` resolves the DSN and the tile URL the
same way `make dev` does.

`curl -s http://127.0.0.1:8001/aois` should list the Mexicali AOI. Open
`http://localhost:5173`, pick it under *Area*, and turn on each layer under
*Layers* at zoom 11–14: the buildings, blocks, roads and municipalities should
render without empty tiles, and the indicator choropleth should colour the
blocks.

## Total wall-clock

About 8 minutes for `scripts/mexicali_run.sh` on an Apple-silicon laptop
(455 s measured, including the 65 MB download), and about 1.1 GB of database.
Rebuilding the bundle adds the INEGI downloads and conversion, an Overture
extract (40 s to 8 min) and an Overpass fetch (minutes).

## Fresh data instead of the bundle

The bundle is the supported input. These commands fetch current data instead,
for rebuilding the bundle or for another area; they need network access, and
`make verify-sources` should pass first. Another INEGI state (for example CDMX,
entidad 09) needs its own local copy of the INEGI products in the bundle's
layout.

**Overture buildings.** The S3 query is rectangular: extract a box, then load
it clipped to the AOI. Pass `--bbox` explicitly — its default is Mexico City's
box, and a Mexicali load of that extract is refused as `ExtractAoiMismatch`:

```sh
uv run python -m apps.cli.app overture extract \
  --bbox=-115.70,32.40,-115.20,32.80 \
  --output-path data/overture_buildings_mexicali.parquet
uv run python -m apps.cli.app overture load \
  --raw-path data/overture_buildings_mexicali.parquet \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id "$AOI_ID"
```

`--release` is omitted deliberately: Overture keeps only its recent releases,
so the default lives in one place, `OVERTURE_DEFAULT_RELEASE`, and a stale one
fails with a message naming what S3 actually has. The box is the urban core,
not the 9,971 km² municipality that reaches San Felipe.

Measured on this box against release 2026-10-21.0 (2026-11-10):

| step | result |
|---|---|
| `overture divisions` | 16 divisions, 2 countries |
| `overture aoi-from-division` | valid MultiPolygon, 2,109 points, 9,971 km2 |
| `overture extract` | 387,273 rows, 51.2 MB, 40 s, **peak RSS 4.4 GB** |
| `overture load` | 387,273 written, **43,577 clipped** outside the Mexican boundary |
| loaded buildings | **343,696**, extent capped at the border (ymax 32.676, from a bbox ymax of 32.80) |

⚠ The 4.4 GB peak is parquet metadata, read while the box is evaluated against
row-group statistics across all 512 files of the release, not the rows (those
cost ~400 MB). `overture extract --low-memory` scans one S3 file at a time:
663 MB peak and 454 s instead of 40 s on the same box. Use it on a laptop also
running Postgres, where 4.4 GB swaps and looks hung.

**An AOI without INEGI data.** Overture's `divisions` theme carries the same
boundaries, in the release the buildings come from:

```sh
# What this release actually calls things, so --name is checkable
uv run python -m apps.cli.app overture divisions

uv run python -m apps.cli.app overture aoi-from-division \
  --aoi-name "Mexicali (Overture municipal boundary)" \
  --subtype county --country MX --name "Municipio de Mexicali"
```

⚠ **Always pass `--country` for a border-crossing box.** The Mexicali box
holds both `MX Municipio de Mexicali` and `US Imperial County`, both
`subtype=county`. The wrong one gives a valid polygon, a successful load and a
map of the wrong country, with no error anywhere.

**Overture places and the OSM network**, fetched live:

```sh
uv run python -m apps.cli.app overture places --country MX --aoi-id "$AOI_ID"
uv run python -m apps.cli.app network bootstrap --aoi-id "$AOI_ID" \
  --clip-bbox=-115.70,32.40,-115.20,32.80 --save-graph data/walk_network
```

`--clip-bbox` narrows the Overpass fetch to the urban box; `--save-graph`
keeps the graph for a bundle or a later `--graph-dir` load.

**WorldPop population raster (optional).** `raster fetch-worldpop` fetches
the country GeoTIFF from WorldPop's REST catalogue (cached under
`data/cache/worldpop/`, ~893 MB for Mexico, 5–10 min the first time) and clips
it to the AOI; `raster ingest` registers it as a COG:

```sh
uv run python -m apps.cli.app raster fetch-worldpop \
  --country MEX --year 2020 --aoi-id "$AOI_ID" \
  --output-path data/worldpop_mexicali_2020_100m.tif
uv run python -m apps.cli.app raster ingest \
  --raw-path data/worldpop_mexicali_2020_100m.tif \
  --layer-slug worldpop_mexicali_2020_100m \
  --aggregation-kind extensive \
  --source-name "WorldPop · MEX · 2020 (100m, Mexicali clip)" \
  --provider WorldPop --version 2020-100m
```

Load fresh features before step 8, or re-run step 8 after them.

## Rolling forward to a new Overture release

When Overture publishes a new release (monthly), set one variable in `.env`
(the built-in default is `OVERTURE_DEFAULT_RELEASE` in
`packages/py/reimagina/pipeline/overture.py`, read once, at import):

```sh
REIMAGINA_OVERTURE_RELEASE=2026-MM-DD.0
```

or, for one shell, `export REIMAGINA_OVERTURE_RELEASE=2026-MM-DD.0`. To find
out what you can pin to, ask S3 rather than guessing — this is also what the
error message prints when a pin has rotted:

```sh
uv run python -c "from reimagina.pipeline.overture import \
available_overture_releases as r; print(r())"
```

`uv run python -m apps.cli.app data verify-sources --release <X>` probes a
release before you set it (`make verify-sources` takes no flags and probes the
default). Loading it under the same `--source-name` replaces the earlier
release: the load deletes that source's previous-release buildings (or places)
inside the extent it just loaded, in the same transaction, so a block never
counts a building twice. It refuses, and loads nothing, if a scenario edit
references one of those features. A different `--source-name` is a different
layer and is left alone. The bundle keeps its own release; a new one is a new
bundle.
