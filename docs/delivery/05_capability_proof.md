---
title: "Capability proof"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — annex to Schedule A"
author: "Rossano Schifanella"
date: "8 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Annex — evidence supporting Works 1 to 4 |
| **Status** | Draft for review |
| **Audience** | A reviewer deciding whether the delivered system does what the documents say |

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | 2026-11-08 | Rossano Schifanella | First complete draft |
| 0.2 | 2026-11-09 | Rossano Schifanella | Current dependency set; GeoParquet 1.1; cross-executor recovery |
| 0.3 | 2026-11-09 | Rossano Schifanella | Run-level deduplication (§13.1) |

### Why this document exists

Schedule A accepts the source code when "the code builds and runs following
the included README", and the technical documentation when it "enables a
technically competent reader to install and run the system". Both criteria are
about behaviour, and neither can be settled by reading a design document.

So this annex records what the system was observed doing. Every number, table
and transcript below was produced on one machine, in one sitting, against a
database that did not exist when the sitting began. Nothing is quoted from a
previous run, a test fixture, or a specification.

It is an annex, not a fifth Work. It adds no contractual obligation and
replaces no part of Works 1 to 4.

### Reproducing it

One command, from a clone with Python dependencies installed and a Postgres
reachable:

```bash
scripts/acceptance_run.sh
```

It drops the configured database, migrates it from empty, generates a
territory, and runs every analysis stage over it. Any failure stops the run.
Section 2 is its output.

A contract test (`tests/contract/test_repository_structure.py`) asserts the
script computes every indicator in `SHIPPED_INDICATORS`, so a capability
cannot be added to the catalogue and quietly left out of the proof.

### What this proof is not

The territory it runs on is **generated, not real**. It is a regular grid on
Mexicali coordinates, and it exists because the ingestion pipelines read a
private Azure data lake that a reviewer has no credentials for. It proves that
the machinery works. It proves nothing whatsoever about Mexicali, and no
number in it should be read as a finding about the city.

Where a generated grid makes an indicator degenerate, that is said plainly —
see §5.

---

## 1. Environment of record

| | |
|---|---|
| PostgreSQL | 16.15 (x86_64-pc-linux-gnu) |
| PostGIS · pgRouting | 3.7.0 · 4.0.2 |
| Python | 3.13.18 (floor 3.12) |
| GeoPandas · Shapely · pyproj | 1.2.0 · 2.2.0 · 3.8.0 |
| rasterio · pyogrio · PyArrow | 1.5.2 · 0.13.0 · 26.0.1 |
| titiler.core · rio-tiler | 2.4.0 · 9.4.13 |
| exactextract · OSMnx | 0.3.0 · 2.1.1 |
| FastAPI · SQLAlchemy · DBOS | 0.162.2 · 2.1.8 · 3.4.0 |
| Migration head | `r047` (46 revisions) |
| Third-party dependencies declared | 259 (`docs/THIRD_PARTY_LICENSES.md`, Article 4.1) — 0 strong copyleft, 8 weak |

Every one of these is the current release at the time of writing. Declared
floors in `pyproject.toml` are the oldest versions CI has run, not the oldest
that might work, so an install without the lockfile still gets software this
project has been tested against.

Whole-run wall time, from `DROP DATABASE` to the last inspection command:
**58 seconds**, measured three times (56.9 s, 58.9 s, 58.3 s).

After migration the database holds:

| Schema | Tables | Views | Materialised views | Functions |
|---|---|---|---|---|
| `curated_geo` | 24 | — | — | 7 |
| `serving_geo` | — | 23 | 1 | 3 |
| `explore` | 1 | 10 | — | — |
| `app_core` | 1 | — | — | — |
| `jobs_runtime` | 5 | — | — | — |

The three-layer separation Work 1 describes is therefore present as structure,
not only as intent: `curated_geo` holds the tables, `serving_geo` is entirely
derived, and `explore` is a separate read surface that no application depends
on.

---

## 2. Installing from nothing

The acceptance run's first four steps:

```
=== 1. Migrating to head
{"app": {"created": true, "migrated": true}, "test": {"created": false, "migrated": true}}

=== 2. Verifying the first-layer schema
{"all_present": true, "checks": {"curated_geo.data_source_catalog": true,
 "curated_geo.feature": true, "curated_geo.feature_type_catalog": true,
 "curated_geo.ingestion_run": true, "serving_geo.feature_v": true}}

=== 3. Seeding the feature-type and indicator catalogs
15 feature types, 11 indicators

=== 4. Generating a territory
  ageb          4 features
  block        16 features
  building     44 features
  establishment     3 features
  aoi          67 member features
  network      25 nodes, 40 edges, 5.21 km
  raster    104x104 px @ ~5.0 m, 22531 people total
```

**What it proves.** A reviewer with no access to the data lake can reach a
populated, queryable system in under a minute. That is the condition Schedule
A's "builds and runs" criterion needs, and before this annex it was not met:
without a road network the three accessibility indicators refuse outright and
the livability score writes zero rows, so a fresh install could exercise seven
of eleven indicators at most.

**Provenance is not optional.** All **67 of 67** generated features carry a
non-null `source_id`, `ingestion_run_id` and `dataset_version`. They were
written through `reimagina.data.feature.load_features`, the same function the
real pipelines use, so they pass the same triggers and carry the same columns.
Nothing in the generator bypasses the loader; the street graph likewise goes
through `reimagina.pipeline.osm_network.load_graph` and the AOI through
`reimagina.data.aoi.create_aoi`.

---

## 3. Cross-grain geometry

```
$ python -m apps.cli.app overlap compute-all --aoi-id <aoi>
  global:building -> MEX:block    44 rows
  MEX:block       -> MEX:ageb     16 rows
  rows_total: 60
```

Every building lands in exactly one block and every block in exactly one
AGEB — 44 and 16 rows for 44 buildings and 16 blocks. The AGEB boundaries are
drawn down the middle of the surrounding street rather than along a block
edge, because on a shared edge "inside" is decided by floating point; the
one-to-one counts are what confirms that choice was necessary.

---

## 4. The indicator catalogue

All eleven shipped indicators, computed in dependency order, 16 rows each:

```
  urban_form_block_compactness             "rows_written": 16
  urban_form_block_building_density        "rows_written": 16
  urban_form_block_planned_floors          "rows_written": 16
  urban_form_block_composite               "rows_written": 16
  edificability_block_housing_units        "rows_written": 16
  edificability_block_population           "rows_written": 16
  edificability_block_density              "rows_written": 16
  accessibility_block_school_network_m     "rows_written": 16
  accessibility_block_health_network_m     "rows_written": 16
  accessibility_block_park_network_m       "rows_written": 16
  livability_block_score                   "rows_written": 16
```

The values, read back from `curated_geo.indicator_result`:

| Indicator | n | min | mean | max | unit |
|---|---:|---:|---:|---:|---|
| `accessibility_block_health_network_m` | 16 | 222.22 | 385.07 | 612.83 | m |
| `accessibility_block_park_network_m` | 16 | 92.12 | 385.22 | 613.13 | m |
| `accessibility_block_school_network_m` | 16 | 222.22 | 385.07 | 743.23 | m |
| `edificability_block_density` | 16 | 266.67 | 580.61 | 923.08 | units_per_ha |
| `edificability_block_housing_units` | 16 | 324.01 | 705.48 | 1121.59 | units |
| `edificability_block_population` | 16 | 1036.83 | 2257.53 | 3589.09 | people |
| `livability_block_score` | 16 | 0.47 | 0.56 | 0.68 | score_0_1 |
| `urban_form_block_building_density` | 16 | 1.65 | 2.26 | 3.29 | buildings_per_ha |
| `urban_form_block_compactness` | 16 | 0.79 | 0.79 | 0.79 | ratio_0_1 |
| `urban_form_block_composite` | 16 | 0.39 | 0.39 | 0.40 | score_0_1 |
| `urban_form_block_planned_floors` | 16 | 2.00 | 4.06 | 6.00 | floors |

---

## 5. Where the generated territory shows through

`urban_form_block_compactness` is 0.79 on every block, to two decimals. That
is correct and it is the data's fault: every generated block is the same
110 m square, and compactness is a shape ratio, so there is nothing for it to
vary with. `urban_form_block_composite`, which reads compactness, is
correspondingly flat at 0.39–0.40.

This is stated rather than hidden because a reviewer who noticed it unaided
would reasonably suspect a stuck computation. Indicators that read varying
inputs do vary: `planned_floors` spans 2 to 6 across five distinct values,
and `building_density` spans 1.65 to 3.29 across twelve.

The accessibility family is the one to look at for evidence that a real
computation is running — see the next section.

---

## 6. Network accessibility through pgRouting

The three `*_network_m` indicators measure metres along the pedestrian graph,
not straight-line distance. The implementation runs **one multi-source
Dijkstra per amenity category**, with every amenity of that category as a
start vertex and `equicost := true` so each reached node is labelled with its
distance to the nearest one. Cost is therefore O(1) shortest-path runs per
category rather than one per block.

Observed on the generated grid — a 520 m square with 25 intersections, 40
edges and 5.21 km of street:

| Indicator | min | mean | max | distinct values |
|---|---:|---:|---:|---:|
| `accessibility_block_park_network_m` | 92.12 | 385.22 | 613.13 | 14 |
| `accessibility_block_health_network_m` | 222.22 | 385.07 | 612.83 | 14 |
| `accessibility_block_school_network_m` | 222.22 | 385.07 | 743.23 | 14 |

Fourteen distinct values over sixteen blocks, with a 650 m spread on a grid
whose diagonal is 735 m. Both snap legs are included — the block centroid's
walk to its nearest vertex and the amenity's own — which is why the shortest
figure (92 m) is well under one block pitch and the longest exceeds the
straight-line distance across the grid.

**What it proves.** The routing stack is wired end to end: an extension
(`pgrouting 4.0.2`), a graph loaded through the production loader, a
multi-source Dijkstra, snap arithmetic on both ends, and three indicators plus
a composite reading the result.

---

## 7. Refusing to guess

An indicator whose input is absent does not return zero. It stops, and says
what is missing and how to load it.

With an empty POI layer:

```
MissingInputData: accessibility_block_school_network_m needs the
establishment (POI) layer, which is empty — every result would be a
confident zero rather than a measurement. Load it with
`apps.cli.app points bootstrap`.
```

With POIs present and the graph truncated:

```
MissingInputData: accessibility (network family) needs
curated_geo.road_edge — the graph to route over, which is empty — every
result would be a confident zero rather than a measurement. Load it with
`reimagina.pipeline.osm_network.bootstrap_network`.
```

**What it proves.** The failure mode this guards against is the expensive one.
A missing road network reported as "1500 m everywhere" produces a plausible
choropleth that an operator would act on. Both refusals name the table, the
consequence, and the loader.

---

## 8. Selective invalidation, measured

The asset graph records which indicator reads which **attribute**, so an edit
recomputes only what depends on it. `POST /jobs/preview` answers that question
without running anything:

| `changed_attributes` | indicators marked dirty | which |
|---|---:|---|
| `["floors"]` | 1 | `urban_form_block_planned_floors` |
| `["geometry"]` | 3 | `building_density`, `compactness`, **`composite`** |
| `["num_levels"]` | 4 | three `edificability_*`, **`livability_block_score`** |
| `["network_version"]` | 4 | three `accessibility_*_network_m`, **`livability_block_score`** |
| `["floors","num_levels"]` | 5 | the union of the two rows above, deduplicated |
| `["not_an_attribute"]` | 0 | — (`"nothing reads what you changed"`) |

The entries in bold are reached transitively: nothing reads `geometry` to
produce `composite`, and nothing reads `num_levels` to produce
`livability_block_score`. They are dirty because a dependency of theirs is.
The two-attribute row returns five, not six, because `livability_block_score`
appears in both sets and is counted once.

**What it proves.** The claim in Work 1 §6.1 — that an edit to `floors` marks
one indicator in eleven rather than all eleven — is measured here, on the
running system, together with the transitive closure that makes the mechanism
non-trivial.

---

## 9. Scenarios: a what-if that cannot corrupt the baseline

Four blocks given six additional floors, then recomputed:

```
  block       baseline  scenario   delta
  SYN0000          2.0       8.0      +6
  SYN0001          3.0       9.0      +6
  SYN0002          4.0      10.0      +6
  SYN0003          5.0      11.0      +6
  SYN0100          3.0       3.0       =
  SYN0101          4.0       4.0       =
  SYN0102          5.0       5.0       =
  SYN0103          6.0       6.0       =
  SYN0200          4.0       4.0       =
  SYN0201          5.0       5.0       =
  SYN0202          6.0       6.0       =
  SYN0203          2.0       2.0       =
  SYN0300          5.0       5.0       =
  SYN0301          6.0       6.0       =
  SYN0302          2.0       2.0       =
  SYN0303          3.0       3.0       =
  blocks the scenario changed: 4 of 16
  current indicator_result rows: 176 baseline, 16 scenario — coexisting, not replaced
  most current rows for any one (feature, indicator, scenario): 1
```

Three properties, each observed rather than argued:

- **The baseline is untouched.** 176 baseline rows (11 indicators × 16 blocks)
  remain current alongside the scenario's 16. No row was overwritten, and zero
  blocks had their base `attributes` written to.
- **Only the dirty indicator was recomputed.** The scenario edited `floors`,
  so it holds rows for `planned_floors` and nothing else — 16, not 176.
- **One current row per tuple.** The partial unique indexes
  (`ir_one_current_base`, `ir_one_current_scenario`) hold at a maximum of 1.

Structurally, this works because `scenario_id` appears on
`curated_geo.scenario_override` and on `indicator_result`, and on neither
`feature` nor `observation`. A scenario has nowhere to write that the baseline
reads.

---

## 10. Live vector tiles, read from the bytes

`serving_geo.get_tile(z, x, y, query_params)` returns Mapbox Vector Tile
bytes. Those bytes were decoded with `scripts/mvt_inspect.py`, which reads the
protobuf wire format directly from the spec and carries no dependency — so the
evidence is "every field the spec names was present and these were the
values", not "a library accepted it".

```
$ python scripts/mvt_inspect.py tile_z14.mvt
layer 'features'  spec v2  extent 4096  54 features
      2  POINT      type_slug=establishment role=poi
      4  POLYGON    type_slug=ageb role=analysis_unit
     12  POLYGON    type_slug=block role=analysis_unit
     36  POLYGON    type_slug=building role=context
  properties: category, floors, id, name, num_floors, num_levels,
              people_per_household, role, syn_pop_ageb_count,
              syn_pop_ageb_sum, syn_pop_count, syn_pop_sum, type_slug,
              unit_size
```

Across zooms, one tile per level over the territory's centroid:

| Zoom | Bytes | Features | What appears |
|---:|---:|---:|---|
| 10 | 1,911 | 20 | AGEBs and blocks only, simplified |
| 12 | 2,465 | 23 | attributes begin to ride along |
| 14 | 4,949 | 54 | buildings and POIs appear; observations merged in |
| 16 | 4,097 | 42 | full geometry, no simplification |

Two behaviours are visible in that table and both are deliberate. Buildings
are withheld below z14, because 10⁵ building polygons in a city-wide tile is
bandwidth spent on something nobody can see. And at z14 the properties include
`syn_pop_sum` — a measured observation, not a stored attribute — because the
tile merges `feature.attributes` with the current observation layer. The
`override ⊕ observation ⊕ base` resolution Work 1 describes is therefore
observable in the delivered tile payload.

### 10.1 Parameters, and failing closed

| `query_params` | Bytes |
|---|---:|
| `{}` | 4,949 |
| `{"type_slug":"block"}` | 1,600 |
| `{"type_slug":"building"}` | 2,720 |
| `{"type_slug":"no_such_type"}` | 0 |
| `{"aoi_id":""}` | 4,949 |
| `{"aoi_id":"<the seeded AOI>"}` | 4,949 |
| `{"aoi_id":"not-a-uuid"}` | **0** |
| `{"aoi_id":"' OR 1=1 --"}` | **0** |
| `{"aoi_id":"00000000-0000-0000-0000-000000000000"}` | **0** |

The last three rows are the point. A malformed or hostile `aoi_id` yields an
**empty** tile, not an error and not the unscoped tile. Empty is the
fail-closed direction: the bug this behaviour replaced was an unrecognised
parameter being ignored, so a scoped request silently returned every feature
in the database. A tile function returns `bytea` and has no error channel, so
raising would reach the client as a broken tile rather than a message.

A blank `aoi_id` reads as "no AOI selected" and stays unscoped, which is how a
client serialises an empty picker.

### 10.2 Scenario-aware tiles

The same tile, with and without `scenario_id`, compared feature by feature on
the value a client would colour by:

```
$ python scripts/mvt_inspect.py --diff baseline.mvt scenario.mvt
feature_id                                 left         right     delta
...
801321d9-df44-4b6f-a6e8-d66936a7e2d5        2.0           8.0        +6
b0b47fd8-5cb5-4ffa-a81d-75e9dabdcde9        4.0          10.0        +6
d13336ca-ea30-4cbc-a9ea-3ce3dd5470cd        3.0           9.0        +6
f7db31ac-b6a5-43f5-87e2-39bcdd15712f        5.0          11.0        +6

4 of 16 features differ between the two tiles
```

And for an indicator the scenario did **not** recompute, the scenario tile is
**byte-identical** to the baseline: 1,384 bytes either way. That is the
designed fallback — a scenario stores only what it changed, so a scenario tile
falls back to the baseline row wherever it has none. Without it, every
indicator the scenario did not touch would be a hole on the map rather than a
baseline value.

The fallback is an anti-join, not `scenario_id = X OR scenario_id IS NULL`.
The widened form looks equivalent and double-counts: where the scenario does
have a row, both it and the baseline row match, and the tile would carry the
same feature twice.

---

## 11. Raster: COG, zonal statistics, and a rollup that conserves

A 104×104 single-band population raster at ~5 m, holding 22,531 people, was
ingested through `raster ingest`:

```
bronze: data/bronze/<source>/…-population.tif        (as received, immutable)
silver: data/silver/<source>/…-population.cog.tif    (re-encoded)
```

The silver file read back with rasterio:

```
driver         GTiff
crs            EPSG:4326
size           104 x 104
internal tiles True 512 512
compression    lzw
overviews      []
nodata         nan
```

Internally tiled and compressed, which is what makes it a COG rather than a
plain GeoTIFF. No overviews, correctly: the whole image is smaller than one
512 px block, so there is no level for the driver to build.

Zonal statistics per block, then rolled up to AGEB by a declarative recipe:

| Grain | Rows | min | max | total |
|---|---:|---:|---:|---:|
| blocks (`syn_pop_sum`) | 16 | 74.0 | 3,010.4 | **16,113.6** |
| AGEBs (`syn_pop_ageb_sum`) | 4 | 4,028.4 | 4,028.4 | **16,113.6** |

The totals agree exactly, which is the property that matters for an extensive
quantity: aggregating people must not create or destroy any. The four AGEBs
are identical because the raster is a symmetric bump and the four quadrants
are congruent — an independent check that the weighting is not favouring a
quadrant.

The 16,113.6 under the blocks against 22,531 across the whole raster is also
consistent, though two denominators are in play and it is worth separating
them. The blocks total 194,410 m²; the territory's feature envelope is
271,532 m² (71.6%) and the raster's footprint is slightly larger again at
292,820 m² (66.4%). The blocks capture 71.5% of the population. The figure
sits above the raster-footprint share because the blocks are nearer the centre
where the surface peaks, and below what pure centrality would give because the
raster extends past them on every side. Both effects are expected; neither
needs to be exact for the rollup conservation above to be the property under
test.

### 11.1 The choropleth zoom floor

| Zoom | Bytes |
|---:|---:|
| 6 | 0 |
| 7 | 0 |
| 8 | 1,320 |
| 9 | 1,320 |
| 10 | 1,320 |

Empty below z8, exactly as `r025` sets it. A block-grain choropleth drawn at
country zoom is a solid colour wash that reads as data.

### 11.2 Raster tiles, and the URL they will not open

The raster endpoint (titiler, mounted under `/cog`) reprojects the COG to
WebMercator on request:

| Tile | Status | Type | Bytes |
|---|---|---|---:|
| `9/91/206` | 200 | image/png | 911 |
| `13/1468/3309` | 200 | image/png | 2,975 |
| `14/2937/6619` | 200 | image/png | 4,475 |
| `16/11748/26478` | 200 | image/png | 8,095 |

The endpoint takes the COG's path as a query parameter, which is the shape of
every server-side request forgery. The guard was probed:

| `url=` | Status |
|---|---|
| the registered COG under `data/silver/` | 200 |
| `file:///etc/passwd` | **403** |
| `file:///workspace/reimagina-evo/../../etc/hosts` | **403** |
| `https://example.com/evil.tif` | **400** |
| `s3://somebucket/evil.tif` | **400** |

```
{"detail": "URL resolves outside the reimagina data root
(/workspace/reimagina-evo/data). Only COGs under that tree are servable."}
```

The traversal case is the one worth noting: the path is **resolved** and then
compared against the data root, so `../../` is rejected after normalisation
rather than by string matching, which is how this class of guard is usually
defeated.

---

## 12. Exports a GIS can open

| Format | Status | `Content-Type` | Bytes |
|---|---|---|---:|
| CSV | 200 | `text/csv; charset=utf-8` | 1,734 |
| GeoJSON | 200 | `application/geo+json` | 8,260 |
| Parquet | 200 | `application/vnd.apache.parquet` | 11,763 |

The media types are the registered ones, not `application/octet-stream`, and
the CSV carries a `Content-Disposition` filename.

The Parquet file is **GeoParquet 1.1 with a covering bbox**, which is the
metadata that makes it readable by GDAL, DuckDB and QGIS without being told
what it is — and skippable by a query engine without decoding it:

```
metadata keys:      ['pandas', 'geo']
geoparquet version: 1.1.0
primary column:     geometry
geometry column:    WKB encoding, ['Polygon'], EPSG:4326
covering:           {'bbox': {'xmin': ['bbox','xmin'], 'ymin': ['bbox','ymin'],
                              'xmax': ['bbox','xmax'], 'ymax': ['bbox','ymax']}}
parquet columns:    feature_id, type_slug, value, quality_flag, computed_at,
                    geometry, bbox
```

The covering is the 1.1 feature that stores each row's bounding box as a
Parquet struct and names it in the file metadata, so a reader can skip row
groups that cannot intersect its query instead of decoding every geometry.

**What it proves.** The analysis leaves the platform in open formats, with the
CRS declared in the file, and in the shape a cloud-native reader can push
predicates into. The asymmetry it closes is worth naming: this platform
already *depends* on that feature when reading — `pipeline/overture.py`
filters the Overture cloud release on its `bbox` struct columns — while
writing plain 1.0, which made it a consumer of cloud-native geospatial and not
a producer of it. The failure was invisible by construction: a file without a
covering opens perfectly in QGIS, it is merely slower to query at scale, and
nothing says so.

---

## 13. Durable execution

The ops plane is DBOS over Postgres: a run is a workflow, its indicator units
are queued tasks, and both are persisted rows rather than in-process state.
The test is to kill the process mid-flight.

```
$ POST /jobs  {"changed_attributes":["network_version"]}
  run 29a1ea84…, 4 units queued
$ sleep 1.2 && kill -9 <worker pid>

--- with no worker alive:
    run running, 0/4 units succeeded
      running     accessibility_block_health_network_m
      running     accessibility_block_park_network_m
      running     accessibility_block_school_network_m
      queued      livability_block_score

--- after restarting the worker:
  22:40:43 INFO (dbos) Recovering 4 workflows from application version 8c6cef31…
    run succeeded, 4/4 units succeeded
```

Three units were mid-execution when the process was destroyed. The restarted
worker reclaimed them from the database and the run completed. Afterwards the
maximum number of current `indicator_result` rows for any one
(feature, indicator, scenario) was **1** — recovery re-ran the work without
double-writing it.

⚠ One caveat on which the mechanism depends: crash recovery reclaims only work
recorded against the executor's own id. The worker warns when
`REIMAGINA_EXECUTOR_ID` is unset and falls back to a stable default, which is
correct for one process per role per host and wrong the moment there are two.
Set it per replica.

### 13.1 A resubmission does not start a second run

Submitting work that is already outstanding returns the run already doing it,
rather than a second run writing the same rows a moment later:

```
resubmit while outstanding          -> same run, deduplicated=True
resubmit after the run is terminal  -> new run
edit an override, same request      -> new run
then resubmit with nothing changed  -> same run, deduplicated=True
```

The third line is the one that decides whether this is correct or merely
present. A scenario edit does not change the request — same attribute, same
features, same scenario — only the values those features resolve to. An
identity built from the request would collapse those two submissions and hand
the operator the run computing the numbers they had just replaced. So the
identity folds in a content fingerprint of everything the planned units read,
taken from the asset graph; editing an override moves it, and restoring the
old value moves it back, because the fingerprint is content addressed rather
than a counter.

### 13.2 A worker that never comes back

The transcript above is a process that *restarts*. A process that **dies** —
a destroyed host, a scaled-away replica — is a different failure, and the
library does not cover it: its PENDING workflows stay recorded against an id
nothing will ever claim, and the run never reaches a terminal state. Nothing
times it out, because from the database's point of view the work is still in
progress.

A live peer reclaims those, gated on a liveness table every executor writes.
Proven the same way — a real `SIGKILL`, but this time the second process
launches under a **different** executor id, so launching recovers nothing and
only the sweep can finish the run:

```
LAUNCHED_AS             recovery-probe-sweeper
UNFINISHED_BEFORE_SWEEP 3
DEAD                    ['recovery-probe-dead']
RECOVERED               3
STATUS                  succeeded
DEAD_HEARTBEAT_ROWS     0
```

⚠ The gate is a correctness requirement, not a courtesy, and the asymmetry
decides its default. Reclaiming a dead executor late costs latency;
reclaiming a *live* one re-enqueues work it is still executing and runs that
indicator twice. So the staleness window defaults to 300 s against a 30 s
beat, and **startup refuses** any configuration where it is below four beats
— the two are separate environment variables, and nothing else would notice
an operator slowing the beat without widening the window.

### 13.3 Dependencies are respected, and parallelism is taken

For a change dirtying two base indicators and one composite:

```
  urban_form_block_building_density    started 22:39:32.836  finished 22:39:33.838  depends_on=[]
  urban_form_block_compactness         started 22:39:32.846  finished 22:39:33.839  depends_on=[]
  urban_form_block_composite           started 22:39:34.094  finished 22:39:35.249
      depends_on=['urban_form_block_building_density', 'urban_form_block_compactness']
```

The two independent units started 10 ms apart and ran concurrently; the
composite started 255 ms after the later of them finished. The DAG is
enforced, and where it permits concurrency the queue uses it.

### 13.4 Progress without polling

`GET /jobs/{run_id}/events` is a server-sent event stream:

```
data: {"run_id": "4e574773…", "indicator": null, "status": "unknown"}
data: {"run_id": "4e574773…", "indicator": "urban_form_block_compactness", "status": "succeeded"}
data: {"run_id": "4e574773…", "indicator": "urban_form_block_building_density", "status": "succeeded"}
data: {"run_id": "4e574773…", "indicator": "urban_form_block_composite", "status": "succeeded"}
data: {"run_id": "4e574773…", "indicator": null, "status": "succeeded"}
```

Per-unit progress as it happens, closing with a run-level terminal event. A
client needs no polling loop.

⚠ The first frame can carry `"status": "unknown"` when the stream is opened in
the same instant as the submit, before the run row has transitioned. It is
cosmetic — the correct status arrives in the next frame — but a client that
renders the first frame verbatim will flash it. Recorded in §15.

---

## 14. The read contract discovers itself

Nothing in §11 required a configuration change. After the raster was ingested
and the recipe run, `GET /tile-sources` reported nine sources, four of them
new:

```
  pmtiles_snapshot      not available   admin_reference
  live_mvt              available       baseline_live
  live_mvt              not available   scenario_overlay
  live_mvt              available       indicator_overlay
  raster_cog            available       syn_pop_acceptance
  aggregation_overlay   available       aggregation_overlay::80e523a8…
  aggregation_overlay   available       observation::syn_pop_ageb_count::ageb
  aggregation_overlay   available       observation::syn_pop_count::block
  aggregation_overlay   available       observation::syn_pop_sum::block
```

Each carries its own URL template, and the aggregation entries carry a version
digest in the query string so a recomputation invalidates the client's tile
cache without a deploy.

**What it proves.** The map's layer list is derived from the catalogue, not
hard-coded in the frontend. Ingesting a new raster or declaring a new
derivation makes a new layer appear. The `not available` rows are equally
informative: the registry distinguishes "implemented but nothing published
yet" from "absent", so an empty map can be diagnosed from one endpoint.

The project API surface is 29 operations; a further 19 come from the mounted
raster tile server, for 48 in total. Work 3 documents them.

---

## 15. Engineer-facing inspection

```
python -m apps.cli.app explore layers
python -m apps.cli.app explore sources
python -m apps.cli.app explore feature-stats
python -m apps.cli.app explore serving-stats
python -m apps.cli.app explore executor-health
```

All five return without error against the seeded database. `executor-health`
reported `executor_id: vm:worker` with a `heartbeat_age` of 21 seconds, which
is how an operator distinguishes "nothing is running" from "nothing was
submitted" — two situations that look identical from the map.

---

## 16. What was not proved here

Stated so that the absence is not mistaken for a claim.

| Not exercised | Why, and what stands in for it |
|---|---|
| Martin, the vector tile HTTP server | The container registry rate-limits anonymous pulls in this environment. Martin's job is to publish `serving_geo.*(z,x,y,query_params) RETURNS bytea` over HTTP; the functions themselves were exercised in SQL and their bytes decoded (§10), which is the part that could be wrong. The HTTP hop is configuration. |
| PMTiles snapshot export | `admin_reference` reports `not available` above — correct, nothing has been exported. PMTiles are for stable reference layers; every layer here is live. |
| API authentication | There is none. It is risk **T1** in Work 1's register and must be closed before any deployment reachable beyond localhost. |
| Scale | The capability transcripts above are a sixteen-block territory. The crash-recovery and sweep proofs ran on a generated 22,500-block, 67,500-building one, seeded in 38 seconds. `docs/architecture/PERFORMANCE.md` owns the benchmark workloads and budgets; this annex is about whether each capability works. |
| The real Mexicali data | Needs data-lake credentials. §2's territory is generated, and §5 says where that shows. |
| GeoArrow encoding | The export is GeoParquet 1.1 with a covering bbox (§12), but geometries are WKB. 1.1's native GeoArrow encoding avoids a decode step on read; geopandas still labels it experimental, so WKB is the interoperable choice today. |
| TypeScript 7 | The web app stays on 5.9. 7.0 is the native compiler port and needs both TS 7 and TS 6 installed plus a `--tsgo` flag, which `svelte-check` is not set up for. Everything else in the web stack is current and `npm audit` is clean. |
| `resolution_m` on raster ingest | Came back `null` for the EPSG:4326 raster — the resolution is in degrees and the field wants metres. Cosmetic in the registry, but it is a null where a number belongs. |
| First SSE frame | Can read `"status": "unknown"` (§13.4). |

---

## 17. Where to look in the code

| Capability | Entry point |
|---|---|
| Acceptance run | `scripts/acceptance_run.sh` |
| Reclaiming a dead executor | `packages/py/reimagina/orchestration/reclaim.py` |
| Territory generator | `scripts/synthetic_territory.py` |
| Tile inspector | `scripts/mvt_inspect.py` |
| Live tiles | `infra/db/migrations/versions/r039_get_tile_aoi_filter.py` |
| Scenario-aware tiles | `infra/db/migrations/versions/r043_scenario_overlay_tile.py` |
| Network accessibility | `packages/py/reimagina/indicators/accessibility.py` |
| Scenario overrides | `packages/py/reimagina/scenarios/overrides.py` |
| Raster ingest | `packages/py/reimagina/pipeline/raster.py` |
| Raster URL guard | `apps/api/cog_security.py` |
| Exports | `apps/api/exports.py` |
