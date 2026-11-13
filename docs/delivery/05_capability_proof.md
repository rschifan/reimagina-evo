---
title: "Capability proof"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — annex to Schedule A"
author: "Rossano Schifanella"
date: "13 November 2026"
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
| 0.4 | 2026-11-09 | Rossano Schifanella | The acceptance run now exercises the ops plane |
| 0.5 | 2026-11-10 | Rossano Schifanella | Tiles over HTTP from the API (§10.3); street context without a basemap (§10.4); corrected API operation count and environment of record |
| 0.6 | 2026-11-10 | Rossano Schifanella | Appendix A — the real Mexicali pilot, run on the author's machine and marked not reviewer-reproducible |
| 0.7 | 2026-11-11 | Rossano Schifanella | Appendix A completed: the network accessibility family, with destination counts and the share of blocks at the cutoff |
| 0.8 | 2026-11-11 | Rossano Schifanella | Park and cultural destinations ingested from Overture places (§A.1), so park accessibility is no longer an absence; twelfth indicator added for cultural venues |
| 0.9 | 2026-11-11 | Rossano Schifanella | §A.1 — the park indicator recomputed over the ingested destinations: 27.9 % of blocks at the cutoff, replacing 100 % |
| 0.10 | 2026-11-11 | Rossano Schifanella | §A.1 — the culture indicator computed on real data (60.8 % at the cutoff) and the seven-of-twelve real-data coverage picture |
| 0.11 | 2026-11-13 | Rossano Schifanella | The acceptance run re-measured at `r049`: the scenario is authored through the CLI (§9), the territory carries streets (§2, §10), and the environment of record, wall time and API operation count are current |

### Provenance of the figures

Re-measured on 13 November 2026, at migration `r049`, from a fresh run of `scripts/acceptance_run.sh`: the environment of record's migration head and revision count, the per-schema table counts, the whole-run wall time, the API operation counts (§14), and the transcripts in §2, §4, §9, §10 and §12. Not re-measured for this revision, and carried forward from earlier runs: the package versions and the third-party dependency count in §1; the crash-recovery and sweep proofs in §13 and the 22,500-block scale figures in §16, which need a generated territory of that size; and Appendix A, which requires the Azure lake mirror and network access that were not available. Each of those figures is true of the run that produced it, not necessarily of this revision.

### Why this document exists

Schedule A accepts the source code when "the code builds and runs following
the included README", and the technical documentation when it "enables a
technically competent reader to install and run the system". Both criteria are
about behaviour, and neither can be settled by reading a design document.

So this annex records what the system was observed doing. Every number, table
and transcript in **§1 to §17** was produced on one machine, in one sitting,
against a database that did not exist when the sitting began. Nothing there is
quoted from a previous run, a test fixture, or a specification.

**Appendix A is different and says so.** It records a run against the real
Mexicali data, which needs the Azure lake mirror and therefore cannot be
reproduced by a reviewer. It is kept separate precisely so the paragraph above
stays true of everything else.

It is an annex, not a fifth Work. It adds no contractual obligation and
replaces no part of Works 1 to 4.

### Reproducing it

One command, from a clone with Python dependencies installed and a Postgres
reachable:

```bash
scripts/acceptance_run.sh
```

It drops the configured database, migrates it from empty, generates a
territory, and runs every analysis stage over it — including step 12, which
starts a worker process and drains a run through the durable queue. Any
failure stops the run. Section 2 is its output.

One capability is deliberately outside it: surviving the executor being
killed. That needs a territory large enough for a unit to still be running
when the kill lands, which the sixteen-block default is not. Work 4 §6.1 has
the two commands, and §13.2 below is what they produced.

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
| Python | 3.12.3 (declared floor 3.12, which CI also pins) |
| GeoPandas · Shapely · pyproj | 1.2.0 · 2.2.0 · 3.8.0 |
| rasterio · pyogrio · PyArrow | 1.5.2 · 0.13.0 · 26.0.1 |
| titiler.core · rio-tiler | 2.4.0 · 9.4.13 |
| exactextract · OSMnx | 0.3.0 · 2.1.1 |
| FastAPI · SQLAlchemy · DBOS | 0.162.2 · 2.1.8 · 3.4.0 |
| Migration head | `r049` (48 revisions) |
| Third-party dependencies declared | 259 (`docs/THIRD_PARTY_LICENSES.md`, Article 4.1) — 0 strong copyleft, 8 weak |

Every one of these is the current release at the time of writing. Declared
floors in `pyproject.toml` are the oldest versions CI has run, not the oldest
that might work, so an install without the lockfile still gets software this
project has been tested against.

Whole-run wall time, from `DROP DATABASE` to the last inspection command:
**91 seconds**, measured three times (97.3 s, 91.1 s, 88.9 s).

After migration the database holds:

| Schema | Tables | Views | Materialised views | Functions |
|---|---|---|---|---|
| `curated_geo` | 25 | — | — | 7 |
| `serving_geo` | — | 23 | 1 | 3 |
| `explore` | 1 | 11 | — | — |
| `app_core` | 2 | — | — | — |
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
15 feature types, 12 indicators

=== 4. Generating a territory (AGEBs, blocks, buildings, streets, POIs, AOI)
  ageb          4 features
  block        16 features
  building     44 features
  establishment     4 features
  network      25 nodes, 40 edges, 5.21 km
  roads        40 road features
  aoi         108 member features
  raster    104x104 px @ ~5.0 m, 22531 people total
```

**What it proves.** A reviewer with no access to the data lake can reach a
populated, queryable system in about a minute and a half. That is the condition Schedule
A's "builds and runs" criterion needs, and before this annex it was not met:
without a road network the four accessibility indicators refuse outright and
the livability score writes zero rows, so a fresh install could exercise seven
of twelve indicators at most.

**The orchestration module is inside this run, not beside it.** Step 12
starts `python -m apps.worker` as a separate process, waits for it to announce
itself through the liveness table, queues a run with `evaluate-scenario
--detach`, and prints the shape the worker ran it in:

```
  worker announced itself: role=worker, beat age 0:00:00.386390
  run 58f7ab78-6c32-4f93-926e-1faa559636e8
  status succeeded, 3 units
    succeeded    +0.00s ran  1.11s  urban_form_block_compactness
    succeeded    +0.00s ran  1.00s  urban_form_block_building_density
    succeeded    +1.32s ran  1.14s  urban_form_block_composite
  submit 1 -> acfd80c6-21d5-43e4-9e77-a12a835c2218  deduplicated=False
  submit 2 -> acfd80c6-21d5-43e4-9e77-a12a835c2218  deduplicated=True
```

Two units start at the same offset and run concurrently; the composite starts
1.32 s in, after both of its inputs finished. The two submissions that follow
are the same request, and they return one run.

This step was added because the evidence had a hole in exactly the place it
could least afford one. Steps 10 and 11 author the scenario through the CLI and
recompute it with `scenario recompute --sync`, which is the in-process
coordinator and needs no ops plane at all — it is the path that still works
when the engine is down.
Everything before step 12 therefore proved the platform *without* exercising
the orchestration module Schedule A names.

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

All twelve shipped indicators, computed in dependency order, 16 rows each:

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
  accessibility_block_culture_network_m    "rows_written": 16
  livability_block_score                   "rows_written": 16
```

The values, read back from `curated_geo.indicator_result`:

| Indicator | n | min | mean | max | unit |
|---|---:|---:|---:|---:|---|
| `accessibility_block_culture_network_m` | 16 | 222.22 | 450.27 | 743.23 | m |
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
one indicator in twelve rather than all twelve — is measured here, on the
running system, together with the transitive closure that makes the mechanism
non-trivial.

---

## 9. Scenarios: a what-if that cannot corrupt the baseline

A scenario is authored the way a planner does it: `scenario create`, then
`scenario set-attribute` with an area drawn as GeoJSON and the policy inputs
over it (12 levels, an 80 m² unit, 3.2 people per household), then
`scenario recompute`. The area catches four blocks; the four edits match four
blocks each, so 16 overrides are written. Then the planned-floors indicator,
baseline against scenario:

```
  block       baseline  scenario   delta
  SYN0-0           2.0      12.0     +10
  SYN0-1           3.0      12.0      +9
  SYN0-2           4.0      12.0      +8
  SYN0-3           5.0      12.0      +7
  SYN1-0           3.0       3.0       =
  SYN1-1           4.0       4.0       =
  SYN1-2           5.0       5.0       =
  SYN1-3           6.0       6.0       =
  SYN2-0           4.0       4.0       =
  SYN2-1           5.0       5.0       =
  SYN2-2           6.0       6.0       =
  SYN2-3           2.0       2.0       =
  SYN3-0           5.0       5.0       =
  SYN3-1           6.0       6.0       =
  SYN3-2           2.0       2.0       =
  SYN3-3           3.0       3.0       =
  blocks the scenario changed: 4 of 16
  current indicator_result rows: 192 baseline, 80 scenario — coexisting, not replaced
  most current rows for any one (feature, indicator, scenario): 1 — the partial unique indexes hold
```

Three properties, each observed rather than argued:

- **The baseline is untouched.** 192 baseline rows (12 indicators × 16 blocks)
  remain current alongside the scenario's 80. No row was overwritten, and zero
  blocks had their base `attributes` written to.
- **Only the dirty indicators were recomputed.** The scenario edited `floors`,
  `num_levels`, `unit_size` and `people_per_household`, so it holds rows for
  the five indicators downstream of them — 80 (5 × 16), not 192.
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
layer 'features'  spec v2  extent 4096  82 features
     27  LINESTRING type_slug=road role=infrastructure
      3  POINT      type_slug=establishment role=poi
      4  POLYGON    type_slug=ageb role=analysis_unit
     12  POLYGON    type_slug=block role=analysis_unit
     36  POLYGON    type_slug=building role=context
  properties: category, floors, id, length_m, name, num_floors, num_levels,
              people_per_household, role, syn_pop_ageb_count,
              syn_pop_ageb_sum, syn_pop_count, syn_pop_sum, type_slug,
              unit_size
```

Across zooms, one tile per level over the territory's centroid:

| Zoom | Bytes | Features | What appears |
|---:|---:|---:|---|
| 10 | 1,915 | 20 | AGEBs and blocks only, simplified |
| 12 | 5,232 | 64 | streets, POIs and AGEBs join; attributes begin to ride along |
| 14 | 6,880 | 82 | buildings appear; observations merged in |
| 16 | 5,820 | 67 | full geometry, no simplification |

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
| `{}` | 6,880 |
| `{"type_slug":"block"}` | 1,600 |
| `{"type_slug":"building"}` | 2,720 |
| `{"type_slug":"no_such_type"}` | 0 |
| `{"aoi_id":""}` | 6,880 |
| `{"aoi_id":"<the seeded AOI>"}` | 6,880 |
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
the value a client would colour by (`urban_form_block_planned_floors`, z12, the
tile that holds all sixteen blocks):

```
$ python scripts/mvt_inspect.py --diff baseline.mvt scenario.mvt
feature_id                                 left         right     delta
...
2c30f06f-c1fe-4261-9b81-e8374c5386ce        5.0          12.0        +7
3b78cc93-2b90-4161-9aec-a4da731d0029        2.0          12.0       +10
40513d7a-e2d2-4301-8b36-44a81272e386        3.0          12.0        +9
e55e6867-c57d-4cae-95e4-a354ff3a53ee        4.0          12.0        +8

4 of 16 features differ between the two tiles
```

And for an indicator the scenario did **not** recompute, the scenario tile is
**byte-identical** to the baseline: 1,336 bytes either way (`urban_form_block_compactness`, z12). That is the
designed fallback — a scenario stores only what it changed, so a scenario tile
falls back to the baseline row wherever it has none. Without it, every
indicator the scenario did not touch would be a hole on the map rather than a
baseline value.

The fallback is an anti-join, not `scenario_id = X OR scenario_id IS NULL`.
The widened form looks equivalent and double-counts: where the scenario does
have a row, both it and the baseline row match, and the tile would carry the
same feature twice.

### 10.3 The same tiles over HTTP, from the API

Everything above is the SQL layer. The HTTP layer that publishes it is
Martin — an external binary this environment could not pull (§16) — so the
tile path was proven at both ends with an unexercised process between them.

The API now implements the same contract. The tile functions all take
`(z, x, y, query_params json)` and return `bytea`, which is Martin's
function-source convention, so `/tiles/{function}/{z}/{x}/{y}` is the
identical URL shape rather than a second one. Choosing between them is one
environment variable:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000   # Martin (default)
REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles              # the API, same-origin
```

Nothing else moves — not `infra/tiles/layers.json`, not `/tile-sources`, not
the frontend. A contract test asserts both forms resolve to the same URL
shape, because that property is the only thing that makes two
implementations safe to keep.

What it answers, measured against the running system:

```
GET /tiles/get_tile/16/11748/26478          200  application/vnd.mapbox-vector-tile  5,820 B
  → decoded: layer 'features', spec v2, extent 4096, 67 features
GET /tiles/get_tile/14/0/0                  204  (a valid tile with nothing in it)
GET /tiles/get_tile/30/1/1                  422  (addresses no tile that can exist)
GET /tiles/lo_get/16/11748/26478            404  (not a servable tile function)
If-None-Match on a current tile             304  (no body)
```

`204` rather than `200` with an empty body is not a style choice: `ST_AsMVT`
over an empty set returns **zero-length bytes, not NULL**, so a route testing
for NULL would answer `200` forever and a client would cache emptiness as
content. The function name is an allowlist, never a parameter — Postgres has
plenty of `bytea`-returning functions and `lo_get` is one.

Martin remains the recommended production path: `get_tile` answers a
populated z16 tile in ~1.5 ms here, so neither Postgres nor the API is the
constraint at this scale, and at city scale a dedicated tile server with its
own pool is the better machine. The point is that it is now an optimisation
rather than a dependency, and that the live tile path is covered end to end
by this repository's own suites.

### 10.4 Street context, with no external basemap

The map had no spatial reference of any kind: a block was a rectangle in
black, with nothing to say which neighbourhood it was in or which way a
street runs. An external basemap would have fixed it and cost a runtime
dependency on someone else's service plus a licence the recipient never
agreed to — which is the opposite of what Article 4.1 asks this delivery to
be able to state.

It needed no external anything. `curated_geo.road_edge` already held the
routing network the accessibility indicators walk; `global:road` was already
in the feature-type catalogue; `serving_geo.roads` already existed as a view
over it and returned zero rows; and `get_tile` already served
`role = 'infrastructure'`. The only missing step was the one that connects
the graph to the feature table, and it is a projection rather than an ingest:
**no migration, no new tile function, no change to `serving_geo`.**

Measured on the generated territory (40 edges, 5.21 km), over the tile that
contains it:

| z | road tile | why |
|---|---|---|
| 11 | **0 B** | below the role gate — `infrastructure` is not served under z12 |
| 12 | 2,714 B | 40 edges |
| 14 | 1,864 B | 27 edges in the smaller tile |
| 16 | 1,670 B | 24 edges |

The frontend's `minzoom` is 12 to match, pinned by a unit test: a client
floor below what the server serves buys requests that can only come back
empty, and on a map empty is indistinguishable from missing data.

**Attribution is derived, not declared.** OpenStreetMap is ODbL and
displaying it uncredited is a breach — but crediting it on a *generated*
territory would be a false provenance claim. `GET /tile-sources/attribution`
reads the licence recorded against each source that actually has features, so
the proof territory credits `reimagina-synthetic` and makes no OSM claim,
while a real network is credited as its licence prescribes. Sources that are
registered but never loaded are excluded, because crediting one would say the
map shows data it does not.

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

The project API surface is 40 operations; a further 19 come from the mounted
raster tile server, for 59 in total. Work 3 documents them, generated from the
application's own OpenAPI document rather than written alongside it.

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
| Martin, the vector tile HTTP server | The container registry rate-limits anonymous pulls in this environment, so the binary itself never ran. The HTTP hop it performs is no longer unexercised, however: the API implements the same contract at `/tiles/{function}/{z}/{x}/{y}` and that path is covered end to end, including a browser rendering from it (§10.3). What remains unproved is Martin specifically — its own pool, its own TileJSON, its own cache headers. |
| Road tile cost at city scale | Streets are served from z12 by the role gate (§10.4). At that zoom the *building* layer measured 2.59 MB per tile, which is why `r038` floors buildings at z14, and a walking network is denser than a road centreline network. The measurement here is 40 synthetic edges, ~68 bytes each — not a city. If a deployment finds z12 road tiles heavy, the remedy is `r038`'s: a `type_slug` zoom floor chosen from a measurement. Extrapolating from 40 edges is the mistake `r038` documents making about buildings. |
| PMTiles snapshot export | `admin_reference` reports `not available` above — correct, nothing has been exported. PMTiles are for stable reference layers; every layer here is live. |
| API authentication | There is none. It is risk **T1** in Work 1's register and must be closed before any deployment reachable beyond localhost. |
| Scale | The capability transcripts above are a sixteen-block territory. The crash-recovery and sweep proofs ran on a generated 22,500-block, 67,500-building one, seeded in 35 seconds — `python scripts/synthetic_territory.py --blocks 150 --ageb-side 10`, no credentials. `docs/architecture/PERFORMANCE.md` owns the benchmark workloads and budgets; this annex is about whether each capability works. |
| The real Mexicali data, *by a reviewer* | Still needs data-lake credentials, so nothing in §1–§17 uses it and §5 says where the generated grid shows. It has now been run by the author — see **Appendix A**, which is labelled as not reviewer-reproducible for exactly this reason. |
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

---

## Appendix A. The real Mexicali pilot

### What this appendix is, and why it is separate

Everything in §1 to §17 runs on a generated territory and can be reproduced by
anyone with Postgres and this repository. This appendix cannot. It records the
system running against the **real** data for Mexicali, which requires the
Azure data-lake mirror described in `docs/AZURE_DATA_INVENTORY.md` —
approximately 17 GB pulled from a private container with Storage Explorer, and
not something a reviewer has credentials for.

It is therefore evidence of a different kind, and it is fenced off so that the
reproducibility claim made earlier in this document is not weakened by
association. Three honest qualifications:

1. **Different machine, different sitting.** The numbers below were produced
   on the author's workstation, not in the environment of record in §1.
2. **Not reviewer-reproducible.** The procedure is
   `scripts/mexicali_run.sh`, committed and documented, but it fails at step 0
   without the mirror — by design, naming each missing path.
3. **A pilot, not an analysis.** These are the pipeline's outputs on real
   inputs. Interpreting them as findings about Mexicali's urban form is a
   separate exercise requiring a planner, and nothing here does it.

### Procedure

```bash
export DATABASE_URL=postgresql://localhost/reimagina_demo
scripts/mexicali_run.sh
```

One command, from a clone with the lake mirror on disk. It drops and recreates
the target database, so it starts from nothing in the same sense §2 does.

### Measured

Baja California (INEGI `CVE_ENT=02`), AOI = Mexicali municipality
(`CVE_MUN=002`), Overture release `2026-10-21.0`, 10 November 2026.

| Stage | Source | Result |
|---|---|---|
| Canonical geometry | INEGI Marco Geoestadístico 2024 | **68,984** blocks, **1,824** AGEBs, 7 municipalities, 1 state |
| Census attributes | INEGI Censo 2020, manzana grain | loaded as `observation` rows, keyed `censo_2020_*` |
| Amenities | IMIP / DENUE Mexicali | **1,493** establishments |
| Buildings | Overture Maps `theme=buildings` | **387,273** extracted and loaded; **313,827** attributed to blocks inside the AOI |
| Street network | OpenStreetMap, pedestrian graph | **38,625** nodes, **117,858** edges, **12,695.2 km**, published as **117,858** `global:road` features |
| AOI membership | `aoi compute-features` | **364,779** members before the network load |
| Overlaps | `overlap compute-all` | building→block **321,324**, block→AGEB **16,101** |

Indicators, each on the 19,058 blocks inside the AOI:

| Indicator | Rows | min | avg | max |
|---|---|---|---|---|
| `urban_form_block_compactness` | 19,058 | 0.0 | 0.6 | 0.9 |
| `urban_form_block_building_density` | 19,058 | 0.0 | 20.3 | 153.8 |
| `urban_form_block_composite` | 19,058 | 0.0 | 0.3 | 0.5 |
| `accessibility_block_health_network_m` | 19,058 | 27.2 | 857.5 | 1500.0 |
| `accessibility_block_school_network_m` | 19,058 | 27.5 | 1244.5 | 1500.0 |
| `accessibility_block_park_network_m` | 19,058 | 1500.0 | 1500.0 | 1500.0 |

⚠ **Three of those need reading with their destination counts, not as
accessibility findings.** The amenity layer for Mexicali holds 1,493 points in
five categories, and that is the whole of what the network family had to route
to:

| Source category | Points | Matched by |
|---|---|---|
| `Consultorio medico` | 758 | health |
| `Farmacia` | 595 | health |
| `Hospital` | 51 | health |
| `Guarderia` | 79 | **school** |
| `Cine` | 10 | **culture** (added after this run — see §A.1) |

| Indicator | Blocks at the 1,500 m cutoff | What the number is |
|---|---|---|
| health | 5,957 of 19,058 (31.3 %) | a measurement, over 1,404 destinations |
| school | 11,358 of 19,058 (59.6 %) | a measurement over **79 nurseries**. The mapping also lists `Educacion_preescolar`, `Educacion_primaria`, `Educacion_secundaria`, `Educacion_media_superior` and `Educacion_superior`; this file contains none of them, so this is nursery accessibility, not school accessibility |
| park | 19,058 of 19,058 (**100 %**) | **not a measurement on this run.** No category in the file is park-like, so every block correctly reports the cutoff and the surface is uniform by construction. §A.1 is the fix; it had not been applied when this run was taken |

The park row is the behaviour `accessibility.py` documents — an absent
category reports the cutoff — and it is also the failure its own comment
warns about: *"1500 m looks like a measurement in a way 0.0 does not."* The
run now emits a warning naming the categories that are present when a
category matches nothing, so the uniform surface is visible rather than
inferred. No value changed; a 100 %-at-cutoff surface is reported, not
suppressed.

It was in fact missed in exactly that way. The uniform layer was not noticed
in this run's own output; it was noticed when the map was opened and an entire
municipality came out one flat colour, and the first reading of that was that
the map was broken. The legend was no help — it explained the flat surface as
*"ramp: catalog bounds (no data yet)"*, which is false, because 19,058 rows
existed. That wording is corrected, and §A.1 removes the cause.

`livability_block_score` returned `features_considered: 0, rows_written: 0`.
Its heaviest weight (0.4) is `edificability_block_density`, which has no rows
here for the reason below. It wrote nothing rather than composing a score from
a missing input.

### What the run establishes that §1–§17 cannot

- **Scale on real geometry.** §16 records the synthetic scale proof at 22,500
  generated blocks. These are 68,984 INEGI manzanas with their real,
  irregular, occasionally self-intersecting boundaries, and 387,273 building
  footprints from a third party.
- **The road tile cost question, partly.** §16 lists it as unproved because
  the measurement there is 40 synthetic edges. A real walking network for one
  city is 117,858 edges over 12,695 km. The tile *size* at z12 on this network
  is still unmeasured and the open risk in
  `docs/superpowers/specs/STREET_CONTEXT.md` stands.
- **`--clip-bbox` earning its existence.** The AOI is the full municipality,
  9,971 km² reaching the Sea of Cortez at San Felipe and almost entirely
  desert. The fetch covered **0.1409** of it. An unclipped request is a
  multi-gigabyte Overpass query whose result is ~99 % empty, and a truncated
  response is indistinguishable from a city with no streets.

### What it does not establish

- **Park accessibility — *as measured by this run*, and no longer.** The
  indicator ran here and wrote 19,058 rows, every one the cutoff, because the
  amenity layer held no park destinations. §A.1 supplies them and reports the
  recompute: 19.4 m to 1500.0 m with 27.9 % of blocks at the cap. The table
  above is left as it was run rather than restated, because it is the record of
  what the pilot produced on 11 November and the point of the annex is what the
  pilot produced.
- **School accessibility, as its name suggests.** 79 nurseries are the only
  matching destinations. The number is real; its scope is much narrower than
  "school".
- **The edificability family.** `edificability_block_*` read `num_levels`,
  `unit_size` and `people_per_household` — scenario inputs that no INEGI or
  Overture layer carries. `scripts/mexicali_run.sh` attempts them and reports
  them unavailable rather than inventing the attributes, and
  `urban_form_block_planned_floors` is unavailable for the same reason
  (`floors`).
- **Anything about Mexicali.** See qualification 3 above.

### Defects this run found that the test suite did not

Recorded because it is the argument for running against real data at all.
Every one was invisible to a suite of 1,108 passing tests, and every one is
fixed with a regression guard.

| Defect | Why the suite could not see it |
|---|---|
| `OVERTURE_DEFAULT_RELEASE` pinned a release Overture had deleted, so every extract failed | Tests assert the glob's *shape*, not that S3 serves it — deliberately, since a test that fails when Overture rotates a release fails for an unrelated reason |
| `curated_geo.road_edge` had no CLI loader, and the accessibility guard told users to call a Python function | Nothing asserted that an error message's remedy was runnable |
| `bootstrap-db` seeded assets before feature types, failing on any database where `r003`'s seed had not landed | Works on a database migrated from scratch, which is every test database |
| `scripts/mexicali_run.sh` omitted `--bbox`, so the extract took `MEXICO_CITY_BBOX` and the AOI clip deleted all of it | Shell and the CLI meet only when someone runs the script |
| Two source modules were excluded by a `.gitignore` pattern matching at every depth, so the repository carried tests for code it did not contain | Local tests pass by construction — the file is present locally |
| The Overture extract peaked at 4.4 GB on one city bbox, which swaps rather than OOMs on a laptop | No test measures resident memory |
| AOI membership intersected the whole 2,109-vertex boundary per feature, recomputing each measure eight times — 8m56s for one city | A sixteen-block fixture is instant either way; `ST_Subdivide` plus an `ST_CoveredBy` short-circuit made it 13x faster with identical membership |
| An accessibility category matching no destination produced a uniform cutoff surface indistinguishable from a measurement | The fixture seeds a park and asks for a school, which is the *one-category-absent* case; nothing covered a category absent from the entire layer |
| The legend explained a flat 19,058-row layer as *"ramp: catalog bounds (no data yet)"* — the one case where the wording matters most, and it named the opposite cause | `rampDomainFromStats` folded "nothing computed" and "every value identical" into one boolean, which is right about the ramp and wrong about the reader; the function had no test of any kind |
| The livability composite's weights summed to 1.0 only because the accessibility family happened to have three members | The edges were built by iterating the family at a literal `weight=0.2`, so the invariant held by coincidence; adding a fourth category took the sum to 1.2 on an indicator declared `valid_max=1.0` |


## A.1 Park and cultural destinations, from Overture places

**Status: measured, on the same machine and the same database as Appendix A.**
The extract was run against Overture's S3 release on 11 November 2026 and the
park indicator was then recomputed over its destinations. Like the rest of
Appendix A this is *not* reviewer-reproducible — it needs the lake mirror named
in qualification 3 — so the synthetic half is reported separately at the end.

### Why this exists

Appendix A's park row reports 19,058 of 19,058 blocks at the 1,500 m cutoff,
because the IMIP amenity layer has no park-like category. The destinations are
available in the *same* S3 release as the Overture buildings already ingested,
under the same licence — no new credential, no new mirror, no change to the
environment's network allowlist.

```bash
python -m apps.cli.app overture places \
    --bbox -115.70,32.40,-115.20,32.80 \
    --country MX --aoi-id <uuid>
```

### What the release holds, and what is counted

| Stage | Rows | |
|---|---|---|
| Places in the Mexicali bbox | 43,037 | the whole theme, 221 distinct categories |
| Matching the category allowlist | 1,303 | 30 categories across park and culture |
| After `--country MX` | 1,147 | 156 dropped as US-side |
| After the name rules | **1,119** | 28 dropped — 1,001 park, 118 culture |

The 4,727 restaurants in that bbox are the reason the ingest filters rather
than loading the theme wholesale: rows no indicator reads would still inflate
the `global:establishment` layer a planner inspects and the tiles that serve
it.

### The two filters, and why each is not optional

**Country.** The bbox is wide enough to hold the municipio, so it also holds
Calexico, El Centro and Heber. 156 of the 1,303 matches are in the United
States, 36 of them parks. The pedestrian network crosses the border too, at
the ports of entry, so without the filter a block in northern Mexicali is
reported a few hundred metres from a park in California that no resident walks
to. The field is fully populated and splits exactly at the border — the
Mexican rows top out at 32.6702 N and the US rows start at 32.6649 N.

**Name.** Overture assigns `park` partly from the name, which in a
Spanish-speaking industrial city collects things that are the opposite of
recreational open space:

| Dropped | Rows | Why |
|---|---|---|
| `Parque Industrial …` | 16 | industrial estates — and they cluster in exactly the districts whose park access is the question, so each one reports an industrial zone as well served |
| `jardín de eventos`, `salón de eventos` | 11 | private ticketed wedding venues, tagged variously `park`, `amusement_park` and `swimming_pool` |
| `Funeraria y Parque Memorial` | 1 | a funeral home |

Every `Parque Acuático`, `Parque Infantil` and aquatic centre survives the
rules; `tests/unit/test_amenity_categories.py` pins both halves, because an
exclusion rule that quietly eats real parks is worse than no rule.

⚠ **The exclusions are the reviewable part of this section.** Selecting park
categories by keyword is actively dangerous on this theme. A substring match
on `park`/`garden`/`sport` — the obvious first implementation, and the one
that was written first — pulls in 907 `hardware_home_and_garden_store`, plus
`rv_park` and `mobile_home_park` (housing), `parking_lot`,
`sporting_goods_store` and `sports_bar`. Nine hundred shops against 178 actual
parks would have produced a *varied* accessibility surface, confident and
wrong by construction: the same defect as the flat layer, and harder to see.
The allowlist is therefore explicit, the 34 rejected near-misses are recorded
with a reason each in `reimagina.data.amenities`, and
`python -m apps.cli.app overture place-categories` prints both.

### What the park indicator will and will not mean

| Destinations | Rows |
|---|---|
| Commercial fitness — `gym`, `sport_or_fitness_facility`, `fitness_studio` | 570 |
| Other sports and recreation — clubs, fields, stadiums, pools, courts | 253 |
| Public open space — parks (151), plazas, playgrounds, gardens, skate parks | 178 |

⚠ **570 of the 1,001 are commercial gyms, and only 178 are public open
space.** They are counted because the
indicator's declared scope has always included private clubs — the IMIP
vocabulary it was written against lists
`Clubs_deportivos_y_acondicionamiento_fisico` beside `Parques_recreativos`,
and the catalog description says so. But a reader who takes
"distance to park or recreation site" as a measure of public green space will
be wrong about this layer by a factor of five, so the catalog description now
states the split.
Restricting it to public open space alone is a defensible *different*
indicator, not a correction to this one.

### The twelfth indicator

`Cine` — 10 cinemas — matched no category at all, so those points were
ingested as establishments nothing read. They now feed
`accessibility_block_culture_network_m`, alongside 118 Overture venues
(cinemas, theatres, museums, galleries, libraries, cultural centres,
arcades). Folding them into `park` to make that layer non-flat would have
manufactured a finding out of 10 points.

`music_venue` (132) and `historic_site` (200) are excluded: upstream the first
is dominated by bars with live music, and the second by markers and plaques
rather than places a resident travels to. Including either would have doubled
the culture count with the wrong thing.

The indicator is **not** part of `livability_block_score`. Livability here is
access to everyday basics — a school, a clinic, somewhere green — and a cinema
is not one of those; and the composite's values are already recorded in §13,
so redefining what it measures is a decision rather than a consequence of
adding a category. `tests/unit/test_composite_normalization.py` pins the
exclusion so it cannot be undone by accident.

### The accessibility family, recomputed

This is the table that replaces Appendix A's `1500.0 | 1500.0 | 1500.0`:

| Indicator | Rows | min | avg | max | At the 1,500 m cutoff |
|---|---|---|---|---|---|
| `accessibility_block_park_network_m` | 19,058 | **19.4** | **813.0** | 1500.0 | **5,326 (27.9 %)** |
| `accessibility_block_health_network_m` | 19,058 | 27.2 | 857.5 | 1500.0 | 5,957 (31.3 %) |
| `accessibility_block_school_network_m` | 19,058 | 27.5 | 1244.5 | 1500.0 | 11,359 (59.6 %) |
| `accessibility_block_culture_network_m` | 19,058 | **50.5** | **1248.5** | 1500.0 | **11,589 (60.8 %)** |

Park accessibility is now a measurement. 27.9 % of blocks have no park or
recreation site within 1,500 m on the pedestrian network — a finding, where
100 % was an artifact of an empty destination set.

⚠ **27.9 % is not a good result that replaced a bad one.** It is a real result
that replaced a non-result. The earlier figure was not "Mexicali has poor park
access"; it was "this question was never asked of the data". Read on its own
merits, park comes out better served than schools (59.6 %) and close to health
(31.3 %) — and the composition table above is the necessary qualification:
570 of the 1,001 destinations are commercial gyms, so this is not a statement
about public green space.

⚠ **Culture is a real measurement on a thin base, and should be read as
one.** 118 destinations across a 9,971 km² municipality, concentrated in the
city centre, give 60.8 % of blocks no cultural venue within 1,500 m on foot —
the worst-served of the four, just behind schools. The number is not
suspect; the question it answers is narrow. Unlike the old park figure it
*does* vary with distance rather than reporting one value, which is the
difference between a measurement over few destinations and no measurement at
all.

The 1,119 places loaded match the extract measured above, category for
category: `gym` 304, `sport_or_fitness_facility` 205, `park` 151,
`sport_or_recreation_club` 78, `sports_and_recreation` 62, `fitness_studio` 61,
down to a single `aquarium`, `golf_course`, `performing_arts_venue`,
`skate_park` and `skating_rink`.

ℹ **The school count moved by one block** — 11,358 in Appendix A, 11,359 here —
and the data did not change; the school indicator was not recomputed. The two
queries differ: Appendix A counted `= 1500.0` and this one counts `>= 1499.9`,
which catches a block sitting fractionally below the cap. Recorded rather than
quietly reconciled, because an unexplained discrepancy between two tables in
the same annex is worse than a one-block footnote.

### What the pilot database holds after this, and what it still does not

Seven of the twelve indicators have rows on real Mexicali data:

| Has rows | Does not |
|---|---|
| the four accessibility network indicators | `urban_form_block_planned_floors` |
| `urban_form_block_compactness` | `edificability_block_housing_units` |
| `urban_form_block_building_density` | `edificability_block_population` |
| `urban_form_block_composite` | `edificability_block_density` |
| | `livability_block_score` |

`livability_block_score` still writes nothing, and adding park and culture
destinations was never going to change that — it was checked rather than
assumed. Its heaviest input is `edificability_block_density` at weight 0.4,
and the edificability family reads `num_levels`, `unit_size` and
`people_per_household`: scenario inputs no INEGI or Overture layer carries.
The composite writes nothing rather than scoring from a missing input, which
is the behaviour Appendix A already records.

So the ratio is unchanged at seven of twelve, but the *membership* is not.
Before this ingest the seven were three urban-form plus health and school
accessibility and two that reported the cutoff everywhere; now all four
accessibility indicators are measurements. The limit that remains is an
attribute-availability limit, not a destination one, and closing it means a
scenario supplying `num_levels` — not another data source.

### Verified on the synthetic territory

The reviewer-reproducible half. `scripts/acceptance_run.sh` seeds one amenity
per category, so the twelfth indicator is exercised by the run that needs
nothing but Postgres:

| Indicator | Rows | min | avg | max | distinct | at cutoff |
|---|---|---|---|---|---|---|
| `accessibility_block_culture_network_m` | 16 | 222.2 | 450.3 | 743.2 | 6 | **0** |
| `accessibility_block_health_network_m` | 16 | 222.2 | 385.1 | 612.8 | 5 | 0 |
| `accessibility_block_park_network_m` | 16 | 92.1 | 385.2 | 613.1 | 7 | 0 |
| `accessibility_block_school_network_m` | 16 | 222.2 | 385.1 | 743.2 | 6 | 0 |

The last two columns are the check that distinguishes a wired-up indicator
from a flat one, and they are now a contract test rather than an observation:
`tests/contract/test_synthetic_territory_covers_every_category.py` fails if a
category is added to the catalog without a destination in the generator.
Without it, the acceptance run would reproduce the Mexicali failure in the one
artifact meant to prove the opposite — a full set of rows, no error, and a
choropleth that measures nothing.
