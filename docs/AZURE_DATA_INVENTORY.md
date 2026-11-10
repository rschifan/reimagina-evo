# Azure data lake inventory and integration plan

Snapshot of `data/sources/azure/` — what was downloaded from
`https://cfcetlsadls.blob.core.windows.net/raw` via Storage Explorer
on 2026-04-25/26 — plus a tiered plan for wiring it into the
reimagina-evo pipeline.

The data lake is **read-only upstream**. Once a dataset is ingested,
its `data_source_catalog.storage_uri` keeps the original Azure URL for
lineage; the pipeline reads from the local Bronze copy.

> ⚠ **`data/` is gitignored** — "anything under `data/` is local cache or
> bootstrap output, never tracked". So no clone carries this mirror, and a
> machine that has not run Storage Explorer against the container has none of
> it. `data verify-sources` reports every path as `missing`, which is the
> expected state of a fresh checkout and not a fault.
>
> **The lake is no longer the only route to Mexicali.** Overture Maps serves
> the municipal boundary (`divisions/division_area`, `MX county Municipio de
> Mexicali`) and 343,696 buildings inside the urban bbox, from public
> anonymous S3, with no credential and no mirror on disk. See
> `docs/BOOTSTRAP.md` §5 and `docs/superpowers/specs/MEXICALI_FROM_OVERTURE.md`.
> What Overture does **not** replace is INEGI: there are no manzanas or AGEBs
> in it, so the block-grain indicators still need this mirror. The Overture
> route gives a real map of a real city; it does not give a Mexicali
> indicator.

## 1. Inventory at a glance

| Category | Path | Size | Format | Coverage |
|---|---|---|---|---|
| **Geometric layers (national)** | `inegi/marco_geostadistico/2024/` | ~3 GB | Hive parquet by `CVE_ENT`, geometry as BLOB (WKB) | All 32 Mexican states |
| **Census attributes (2010)** | `inegi/censo/manzanas_2010.parquet/` | ~1.6 GB | Hive parquet by `entidad/mun`, **199 columns** (pobtot, age cohorts, education, housing) | All Mexico, block grain |
| **National business registry** | `denue/denue_estimaciones_mx_2023.parquet/` | 360 MB | Hive parquet by `cve_ent`, 43 columns (business name, NAICS, employee band, address) | All Mexico, point-level |
| **Mexicali amenities** | `exe/002_mexicali/equipamientos.geoparquet` + `por_estado/02_baja_california/002_mexicali/imip/equipamiento/*.geoparquet` (14 categories) + `equipamiento_combined.fgb` | 2.8 MB | GeoParquet + FlatGeobuf, points, EPSG:4326 | Mexicali |
| **CDMX-Tlalpan amenities** | `sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet` (1785 pts) + `inegi/denue/amenities/exe_amenities_denue*.geoparquet` (1493 pts) | 76 KB | GeoParquet, points, EPSG:4326 | CDMX-Tlalpan |
| **Population grids 2020** | `population_grids/mun_2020.fgb` | 55 MB | FlatGeobuf, MultiPolygon, EPSG:6372 | Mexican municipalities, `CVEGEO + POBTOT` |
| **GHSL built-up raster** | `ghsl/built_surface/bbox_clip_19_039_.tif` | 172 KB | GeoTIFF, 100 m, ESRI:54009 (Mollweide), `uint16` | Tiny 40×30 km clip — Nuevo León / Monterrey area (sample, **not CDMX**) |
| **Mobile phone data (Mexicali)** | `phone-data/ciudad=mexicali/year=2024/month=*/day=*/part-*.snappy.parquet` | 7.7 GB | Spark/Delta partitioned parquet, daily, ~Oct–Nov 2024 | Mexicali, 23-col schema (anonymised aid, lat/lon, timestamp, OS, carrier) |

### Schema highlights worth knowing

- **`inegi/marco_geostadistico/2024/manzanas_urbanas.parquet`**:
  `CVEGEO, CVE_LOC, CVE_AGEB, CVE_MZA, AMBITO, TIPOMZA, geometry, CVE_ENT, CVE_MUN`
  — geometry stored as BLOB (WKB), partitioned by `CVE_ENT`.
- **`inegi/censo/manzanas_2010.parquet`**: 199 columns. Joins to
  `manzanas_urbanas` via the implicit `(entidad, mun, loc, ageb, mza)`
  composite or via a derived CVEGEO. Confirm the join key during ingest.
- **All amenity geoparquets** share the same column set:
  `id, category, capacity, geometry, fuente, fecha, entidad` — clean
  to merge into one canonical `amenity_point` feature type.
- **`mun_2020.fgb`**: `CVEGEO, POBTOT, geometry`. This is the
  2020 census population at municipality grain — useful as a quick
  modern population layer until block-level 2020 surfaces.
- **GHSL raster**: ESRI:54009 (World Mollweide), needs reprojection
  to EPSG:4326 / 32614 before joining with the rest.

### Pipeline pilot scope shifts

The current prototype is CDMX-Cuauhtémoc-only. The lake covers:

- **All of Mexico** at INEGI / DENUE national layers,
- **CDMX-Tlalpan** with its own amenities (different alcaldía),
- **Mexicali** (Baja California) with amenities + mobile phone data,
- **A Monterrey-area sample raster** for GHSL.

Three pilot cities materialise from this: **CDMX-Cuauhtémoc** (already
shipped), **CDMX-Tlalpan**, **Mexicali**. The pipeline already
generalises by `feature_type` + AOI — no architectural change needed,
but seeds and AOI rows must be added.

## 2. Quirks and follow-ups before ingesting

1. **Geometry decoding.** `marco_geostadistico` stores `geometry` as
   `BLOB`, not GeoParquet. `geopandas.read_parquet` will not pick it
   up automatically — read with `pyarrow`, then construct a GeoSeries
   from `shapely.from_wkb`.

2. **Vintage mismatch.** Census is **2010** at block grain;
   `mun_2020.fgb` is 2020 at municipality grain. We can't roll up
   2010 attributes to 2020 boundaries cleanly — they're different
   geometries. Treat them as two separate observation snapshots.

3. **Amenity duplication.**
   `exe/002_mexicali/equipamientos.geoparquet` and
   `por_estado/02_baja_california/002_mexicali/imip/equipamiento_combined.fgb`
   look like two formats of the same dataset. Pick one canonical
   source per pilot city; archive the other in `data/sources/azure/`
   without ingesting.

4. **GHSL clip.** `bbox_clip_19_039_.tif` is a Monterrey sample,
   not CDMX. Useful as a raster-pipeline smoke test against a
   non-WorldPop source but **not** a baseline layer for any current
   pilot. Don't rush to ingest.

5. **Phone data.** Massive (7.7 GB), per-day, requires special
   handling (privacy, partitioning, possibly downsampling). Defer to
   a Layer 2/3 milestone — no need to wire it in for the data layer.

6. **All Mexico vs the pilot AOIs.** Loading every state into
   `curated_geo.feature` for AGEBs and blocks would be
   ~tens of millions of rows. Filter at ingest by the AOI's
   `CVE_ENT` (e.g. `09` for CDMX, `02` for Baja California) — the
   Hive partitions make this trivial.

## 3. Integration plan — three tiers

### Tier 1 — Foundation (do first)

Replace and extend the baseline so all three pilot cities work the
same way the existing CDMX-Cuauhtémoc one does.

| # | Step | Source | Pipeline target |
|---|---|---|---|
| 1.1 | Add Mexicali (`02_002`) and Tlalpan (`09_012`) to `app_core.aoi` (or equivalent AOI table) alongside the existing Cuauhtémoc seed. | hard-coded | `infra/db/seeds` |
| 1.2 | Ingest `manzanas_urbanas`, `agebs_urbanas`, `areas_municipales`, `areas_estatales` from `inegi/marco_geostadistico/2024/`, **filtered to the three pilot states** (`CVE_ENT IN ('02','09')`), as `feature` rows of types `block`, `ageb`, `municipality`, `state`. | `inegi/marco_geostadistico/2024/*.parquet` | `pipeline.silver.read_vector` (extended for BLOB geometry) → `pipeline.loader.load_vector_source` |
| 1.3 | Compute `feature_overlap` for `block→ageb`, `block→municipality` across the new AOIs. | (derived) | existing `jobs/overlap.py` |
| 1.4 | Smoke test: existing CDMX-Cuauhtémoc indicators still render; Tlalpan + Mexicali AGEBs visible in the web app sidebar. | — | manual |

**One big win**: this gives the prototype a **national-grade
geometric backbone** in place of whatever Overture/INEGI subset is
currently in. Future cities cost ~5 minutes (just add an AOI row).

### Tier 2 — Census enrichment + amenities

Now that features exist, layer attributes on top.

| # | Step | Source | Pipeline target |
|---|---|---|---|
| 2.1 | Ingest INEGI 2010 census per-block attributes as `observation` rows. Start with the headline columns (`pobtot, pobmas, pobfem, p_15ymas, vivtot, vivpar_hab`); add the rest later if useful. | `inegi/censo/manzanas_2010.parquet` | `pipeline.tabular` (new shape, mirrors raster zonal-stats) |
| 2.2 | Roll up census observations to AGEB and municipality grains via `aggregation_recipe` (sum). | (derived) | existing `jobs/observation_rollup.py` |
| 2.3 | Ingest `mun_2020.fgb` as a single `observation` snapshot for `attribute_key='pobtot_2020', feature_type='municipality'`. Side-by-side with 2010 — different snapshot, different vintage. | `population_grids/mun_2020.fgb` | `pipeline.silver.read_vector` |
| 2.4 | Ingest the canonical amenity layer per pilot city: Mexicali → `exe/002_mexicali/equipamientos.geoparquet`; Tlalpan → `sigcdmx/amenities/exe_amenities_tlalpan_complete.geoparquet`. As `feature` of new type `amenity_point`. | (above) | new `pipeline.points.py` (similar to vector loader, no overlap computation needed) |
| 2.5 | Define an indicator `amenity_count` per block / AGEB via `aggregation_recipe` of input_kind `feature_count`. Renderable as a choropleth. | (derived) | existing aggregation layer (extend recipe vocab if `feature_count` not yet a kind) |

After this tier, the web app shows: who lives where (2010 + 2020),
where amenities are clustered, and a derived "amenity density"
indicator — all per pilot city.

### Tier 3 — Stretch

Defer until Tier 1 + 2 are stable.

- **DENUE national.** 360 MB national parquet with detailed business
  classifications. Could become a richer amenity layer (filtered by
  NAICS code for schools, clinics, transit, etc.) replacing the small
  pre-categorised geoparquets. Geometry: needs lat/lon columns
  (verify schema) or geocoding from address fields.
- **GHSL raster.** Use as a non-WorldPop smoke test for the raster
  pipeline. Reproject to UTM-14N, run zonal stats against block
  geometries — confirms the raster path doesn't depend on WorldPop's
  exact CRS/dtype.
- **Mobile phone data (Mexicali).** Defer entirely until a privacy
  + aggregation policy is agreed. Likely lands in a separate
  `mobility` schema with explicit access controls. Indicators it
  could power: weekday/weekend visit ratios, OD matrices,
  amenity attractiveness scoring. Heavy work — own milestone.

## 4a. Recipe — seed the three pilot AOIs

After Tier 1.2 has loaded municipalities for `CVE_ENT IN ('02','09')`:

```bash
# CDMX-Cuauhtémoc — already shipped, kept for completeness.
reimagina-cli cdmx aoi-from-alcaldia \
    --cve-ent 09 --cve-mun 015 --name "CDMX · Cuauhtémoc"

# CDMX-Tlalpan — second alcaldía, free given INEGI national coverage.
reimagina-cli cdmx aoi-from-alcaldia \
    --cve-ent 09 --cve-mun 012 --name "CDMX · Tlalpan"

# Mexicali — Baja California, paired with phone-data + amenities in the lake.
reimagina-cli cdmx aoi-from-alcaldia \
    --cve-ent 02 --cve-mun 002 --name "Mexicali" \
    --project-name "Reimagina pilots"
```

The subcommand still says `cdmx aoi-from-alcaldia` for back-compat, but
`--cve-ent` makes it state-agnostic — Mexicali is just a different
entidad code. A future PR may rename the subcommand to
`aoi from-municipality`.

## 4b. Recipe — load amenity points

After Tier 1.6 has shipped, the per-pilot-city POIs land with one CLI call:

```bash
# Both Tlalpan + Mexicali amenity layers
reimagina-cli points bootstrap

# Or pick one
reimagina-cli points bootstrap --layers tlalpan_amenities
```

Features land as `global:establishment` with attributes
`{category, capacity, fuente, fecha, entidad}` in `feature.attributes`.
A future migration may add a more specific `amenity_point` feature type
when the precision actually earns its keep — until then the existing
POI type is the right home.

## 4. Concrete next step

Tier 1 is the unblock. Recommend starting with **step 1.2** — the
INEGI marco geostadístico ingest — because everything downstream
depends on having `feature` rows to attach observations to.

Suggested first PR: a thin `pipeline.inegi_marco` module that reads
one `entidad` partition of `manzanas_urbanas.parquet`, decodes the
WKB geometry column, and inserts into `curated_geo.feature` with
`feature_type='block'`. One state, one feature type — proves the
shape, then generalise to AGEB / municipality / state and to all
three pilot states in follow-ups.

## 5. Out of scope

- Writing back to the data lake (read-only invariant; see
  `docs/SYSTEM_OVERVIEW.md` and the system memory).
- Re-pulling data via Azure CLI / SDK — auth was blocked by tenant
  issues; manual Storage Explorer downloads are the operational path.
- Schema changes to `data_source_catalog`. The existing `storage_uri`
  text column already accepts `azure://...` URIs.
- Cross-vintage rollups (2010 census → 2020 boundaries). Treat the
  two as separate snapshots until the team decides on a reconciliation
  approach.
