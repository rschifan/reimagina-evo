---
title: "Software and systems architecture map"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — Schedule A, Work 1"
author: "Rossano Schifanella"
date: "20 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Schedule A, Work 1 — Software and systems architecture map |
| **Version** | 1.0 |
| **Status** | Issued |
| **Delivery identification** | Date, release tag, commit and SHA-256 of every delivered file are recorded in `docs/delivery/MANIFEST.md`, as Article 2.2 of the agreement requires |
| **Audience** | Technical reviewers, maintainers and operators of the platform |

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | 2026-11-08 | Rossano Schifanella | First complete draft |
| 0.2 | 2026-11-08 | Rossano Schifanella | Indicator count corrected to eleven, measured against the running system |
| 0.3 | 2026-11-09 | Rossano Schifanella | Orchestration module documented (§5.5, Figure 5); current dependency set |
| 0.4 | 2026-11-09 | Rossano Schifanella | Run-level deduplication |
| 0.5 | 2026-11-10 | Rossano Schifanella | Constraint C6 (no third-party runtime service); the tile server is interchangeable (§5.1); street context in place of a basemap (§3.3) |
| 0.6 | 2026-11-11 | Rossano Schifanella | Re-issued with the 11 November delivery; no change to this document's content |
| 0.7 | 2026-11-13 | Rossano Schifanella | T3 resolved: a scenario edit can now be authored through the API, the CLI and the map |
| 1.0 | 2026-11-20 | Rossano Schifanella | Issued for delivery, checked against migration `r051`; the corrections are listed below |

Corrections in version 1.0:

- External integrations: Overture, OpenStreetMap, WorldPop and the INEGI and CDMX portals are called directly at ingestion; the private data lake is an offline mirror the code never calls (Figures 2 and 3, §2, §3).
- A new end-to-end data-flow figure (§6.1, Figure 6).
- The worker runs indicator units only; pipelines and derivation jobs run in the CLI (§5.3, Figure 4).
- Scenario rules are recorded in `scenario_edit` and materialised into `scenario_override` on recompute (§6.2, Figure 7).
- The orchestration port has six methods and two implementations (§5.5, Figure 5).
- Deployment: pgbouncer 1.26.1, the API's direct connection for the ops plane, plain HTTP, `make dev` serving the pages through Vite, API and worker as host processes, ports (§7, Figure 8).
- The schema rule restated against what the code reads (§8.1); 24 geometry columns.
- The web application, its four pages and the API's endpoint groups described (§5.1, §5.2).
- The indicator catalogue holds twelve indicators, not the eleven of version 0.2; test counts removed.

### Provenance of the figures

This document describes the system at migration `r051` as of 20 November 2026. It carries no timings and no test counts; those are in the technical documentation.

### About this document

This document follows **arc42**, the open template for software architecture
documentation, whose twelve sections each hold one kind of architectural
information. Sections that carry little content for this system are kept
short rather than padded, which arc42 explicitly permits.

The diagrams follow the **C4 model**. Each carries its figure number and a
title naming its type and scope inside the image, so a figure is never
separated from its caption. Each element states its type and — for
containers and components — its technology. In the structural diagrams every
relationship is a labelled, one-way line that names the protocol where
processes communicate; in the dynamic diagram (Figure 7) the arrows mean
"then" and the steps are numbered instead. The notation key in §3.1 applies
to every figure.

Everything factual here was derived from the delivered system: component
responsibilities from the packages themselves, schema facts from a database
migrated to `r051`, endpoints from the application's own OpenAPI document,
runtime topology from the deployment configuration and the `Makefile`.
**Where this document and the code disagree, the code is correct and this
document is the defect.**

---

## 1. Introduction and Goals

Reimagina is an urban-planning data platform. It ingests public and
proprietary geospatial data about a territory, normalises it into one
canonical model, computes urban indicators over it, and serves the result to
a web map. On top of that baseline it evaluates **scenarios** — hypothetical
edits whose effect on the indicators is computed and compared against the
city as it stands. The delivered configuration covers two pilot territories:
Tlalpan, in Mexico City, and Mexicali.

### 1.1 Requirements overview

| # | Requirement |
|---|---|
| R1 | Ingest heterogeneous territorial data — boundaries, census, amenities, buildings, rasters, road networks — into a single canonical model |
| R2 | Compute urban indicators over that model, at the grain the analysis requires |
| R3 | Let an analyst pose a what-if scenario and see its effect on the indicators |
| R4 | Serve the territory and its indicators to an interactive web map |
| R5 | Make ingestion and recomputation observable and repeatable by an operator |

### 1.2 Quality goals

In priority order. These are the goals the architecture is shaped around;
§10 records how each is judged.

| # | Quality goal | What it means here |
|---|---|---|
| Q1 | **Correctness of derived numbers** | An indicator must be reproducible from its inputs and must not silently drift when those inputs change |
| Q2 | **Integrity of the baseline** | No scenario, however authored, may alter the city as recorded |
| Q3 | **Inspectability** | An engineer must be able to see what the system holds and why, without reading application code |
| Q4 | **Efficient recomputation** | An edit must recompute what it affects, not everything |
| Q5 | **Operability** | A run must be observable, cancellable and resumable |

### 1.3 Stakeholders

| Role | Expectation of the architecture |
|---|---|
| Urban planner / analyst | Trustworthy numbers; a scenario comparison that is quick enough to explore with |
| Platform operator | Repeatable ingestion; visible, controllable runs |
| Maintainer | A model that can absorb a new indicator or data source without a redesign |

## 2. Constraints

| # | Constraint | Consequence |
|---|---|---|
| C1 | Part of the source data exists only in a private data lake, copied by hand to local disk | The code holds no credential and never calls the lake: the pipelines read the mirror under `data/sources/azure/`. Without it the platform still installs, runs and loads a territory from the public sources (§3.3) or from `scripts/synthetic_territory.py`; what is lost is INEGI's 2024 block and AGEB geometry, the 2020 census attributes and the two amenity layers |
| C2 | Spatial work is done in PostGIS, not in application memory | Spatial measures — areas, lengths, distances, network walks — are SQL; the database is the computational centre, not a passive store |
| C3 | Network distance requires pgRouting | pgRouting is a hard dependency, and it is not a *trusted* Postgres extension, so installation needs a superuser |
| C4 | One coordinate reference system for vector data | Every geometry column is SRID 4326, enforced by the column type. Rasters keep their native CRS; zonal statistics reproject the feature geometries to the raster instead |
| C5 | The platform is delivered as source, to be run by the recipient | Installation must be reproducible from the repository by a competent reader |
| C6 | No third-party service may be required to *serve* the platform | Ingestion calls public services (§3.3); once data is loaded, the map, the API and the runs need none. This rules out a hosted basemap, which would add a runtime dependency and a licence the agreement's Article 4.1 would have to carry. Spatial context is drawn from the platform's own street network instead |

## 3. Context and Scope

### 3.1 Notation key

Figure 1 is the notation shared by every figure that follows: the colour of
an element gives its C4 type, and the style of a line says whether it is an
always-on call.

![Notation key](diagrams/c4_00_legend.svg)

### 3.2 System context

Reimagina sits between the publishers whose datasets describe the territory
and the people who reason about it. Every external system in Figure 2 is
reached only at ingestion time, by the operator CLI; none is on the path of a
map request or a recomputation. Nothing is written upstream.

The private data lake is drawn dashed because the system never calls it. An
operator copied its contents to `data/sources/azure/` with a desktop storage
client, and the pipelines read that local mirror; `data_source_catalog`
still records the original lake path for lineage.

![System context](diagrams/c4_01_system_context.svg)

### 3.3 External interfaces

Ingestion, all initiated by the operator CLI:

| External system | Protocol | Code | Data |
|---|---|---|---|
| Overture Maps | Anonymous S3 (`s3://overturemaps-us-west-2/release`), GeoParquet | `pipeline/overture.py`, `overture_places.py`, `overture_divisions.py` | Buildings, places, administrative divisions |
| OpenStreetMap | Overpass API over HTTPS, through `osmnx` | `pipeline/osm_network.py` | Walking network, loaded into `road_node` / `road_edge` |
| WorldPop | REST API over HTTPS | `pipeline/worldpop.py` | Gridded population rasters |
| INEGI and CDMX portals | HTTPS downloads: an `inegi.org.mx` archive and the SIGCDMX ArcGIS REST service | `pipeline/inegi.py`, `pipeline/cdmx.py` | CDMX marco geoestadístico (municipalities, AGEBs, blocks), colonias |
| Private data lake (mirror) | None at run time: local files | `pipeline/inegi_marco.py`, `inegi_censo.py`, `points.py` | INEGI marco 2024, INEGI census 2020, Tlalpan (SIGCDMX) and Mexicali amenities |

`make verify-sources` checks the lake mirror, Overture and WorldPop before
an ingestion is started. The PMTiles export shells out to GDAL's `ogr2ogr`, a local tool
rather than a service.

Serving, to clients on the same host or network:

| Interface | Protocol | Purpose |
|---|---|---|
| Web pages | HTTP | The four pages of §5.2 |
| REST API | JSON over HTTP; Server-Sent Events | Features, indicators, AOIs, scenarios, runs and their progress, downloads (CSV, GeoJSON, Parquet), data attribution |
| Vector tiles | MVT over HTTP | Live map layers, from Martin or, behind the same URL contract, from the API (§5.1) |
| Raster tiles | HTTP | Cloud-Optimized GeoTIFF tiles, served in-process by the API under `/cog` |
| Snapshot tiles | PMTiles over HTTP | Exports of stable layers for GIS clients, from the nginx container; the web pages do not use them |

All of it is plain HTTP. HTTPS, like authentication (T1), belongs to a
reverse proxy the operator places in front of a networked deployment.

There is no external basemap, and that is a constraint rather than an
omission (C6). The routing graph is published as `global:road` features and
served through the same tile path as every other layer. Because those
streets are OpenStreetMap data under the ODbL, the API derives the credit
line from the data actually loaded (`GET /tile-sources/attribution`) and the
map displays it.

Build and verification integrations, outside the delivered runtime:
GitHub Actions runs the CI workflow (§8.5); Python packages come from PyPI
through the locked `uv.lock`, web packages from the npm registry through
`package-lock.json`; the database image comes from Docker Hub
(`pgrouting/pgrouting`, `edoburu/pgbouncer`, `nginx`) and Martin from
`ghcr.io`.

## 4. Solution Strategy

Five decisions shape everything else. §9 records why each was taken.

| Decision | Serving |
|---|---|
| Keep the spatial computation in PostGIS, and the indicator arithmetic in small, checked Python kernels | Q1, C2 |
| Separate the source of truth from what the application reads, using distinct schemas | Q1, Q3 |
| Record scenario edits as overrides resolved at read time, never as mutations of the baseline | Q2 |
| Drive recomputation from a dependency graph over attributes | Q4 |
| Run the ops plane as durable execution inside the same database | Q5 |

## 5. Building Block View

### 5.1 Level 1 — Containers

Figure 3 shows the seven containers. Three details in it are deliberate
rather than incidental. The tile server reads the same serving views the API
reads, so a map and a table can never disagree about what the city contains.
The worker talks to the database directly rather than through the pooler,
because the durable-execution library's checkpoints and progress streams use
`LISTEN`/`NOTIFY`, which cannot cross a transaction-mode pooler. And the API
holds **two** kinds of connection: a pool for its ordinary reads and writes,
and a direct one, through the library's client, for enqueuing runs, writing
their plan and streaming their progress to `GET /jobs/{run_id}/events`.

![Container diagram](diagrams/c4_02_containers.svg)

**The API's pool is bounded and waits.** One process-wide
`ThreadedConnectionPool` holds at most 16 connections
(`REIMAGINA_API_POOL_MAX`). A request that finds them all busy waits up to
30 seconds (`REIMAGINA_API_POOL_WAIT_S`) and is then answered `503` with
`Retry-After`, rather than failing at once or hanging.

**The API's surface.** The OpenAPI document lists 62 operations on 55 paths:
43 are the platform's own and 19 are titiler's raster endpoints under
`/cog`. The platform's operations fall into these groups:

| Group | Paths | Purpose |
|---|---|---|
| Health | `/health`, `/health/live`, `/health/ready`, `/health/tile-server` | Liveness, readiness, tile-server reachability |
| Indicators | `/indicators`, `/indicators/{slug}`, `…/dependencies`, `…/lineage`, `…/values`, `…/stats`, `…/values.csv`, `…/values.geojson`, `…/values.parquet` | Catalogue, lineage, values, statistics, downloads |
| Places | `/aois`, `/aois/{aoi_id}/summaries`, `/features`, `/features/{feature_id}/indicators`, `/metrics/within-polygon`, `/metrics/indicator-within-polygon` | Areas of interest, features, a block's values and rank, statistics inside a drawn polygon |
| Tiles | `/tile-sources`, `/tile-sources/attribution`, `/tiles/{function}`, `/tiles/{function}/{z}/{x}/{y}` | Layer descriptions, data credits, TileJSON and MVT |
| Runs | `/jobs` (submit, list, clear), `/jobs/attributes`, `/jobs/preview`, `/jobs/{run_id}` (read, delete), `…/detail`, `…/events`, `…/cancel`, `…/rerun` | Submit, preview, observe, cancel and re-run recomputation |
| Scenarios | `/scenarios` (list, create), `/scenarios/indicators`, `/scenarios/{scenario_id}` (read, delete), `…/edits` (add, clear), `…/recompute`, `…/comparison` | Author, recompute and compare scenarios |
| Raster | `/cog/…` (19 operations, titiler) | COG metadata, statistics and tiles |

**The tile server is interchangeable.** The contract is the SQL function
signature — every tile function takes `(z, x, y, query_params json)` and
returns `bytea`, which is Martin's function-source convention — so anything
implementing it is a drop-in. Three such functions exist in `serving_geo`:
`get_tile` (features), `indicator_overlay_tile` (indicator values, baseline
or scenario) and `aggregation_overlay_tile` (values aggregated to a coarser
grain). Martin publishes every function in `serving_geo`; the API implements
the same contract at `/tiles/{function}/{z}/{x}/{y}`, accepting only those
three names. Which one the web pages use is one environment variable,
`REIMAGINA_TILE_LIVE_BASE_URL`. The code's default is Martin
(`http://localhost:3000/get_tile`), the recommended production path; `make
dev` sets it to `/api/tiles`, because it does not start Martin, so the
development map is served by the API through the Vite proxy. The reason for
having both is that Martin is an external binary: with only Martin, the live
tile path has an unexercised process in the middle of it, which for a
delivery accepted on "the code builds and runs" is the weakest link in it.

### 5.2 The web application

Four pages, built by Vite 8 as one multi-page application from
`apps/web`. The map page is TypeScript over MapLibre GL 6; the other three
are Svelte 5 applications styled with Tailwind 4, using bits-ui components,
and the scenario workspace draws areas with terra-draw. A shared top bar
links the four.

| Page | File | What it does |
|---|---|---|
| Map | `index.html` | Explores the territory. Layers start switched off; the first view lands where the chosen indicator draws. The legend sits on the map and shows the colour ramp and the share of blocks at the cap. Clicking a block pins an inspector with its values and its rank in the area. Drawing a selection shows a summary of the indicator inside it (`/metrics/indicator-within-polygon`), with the previous numbers kept on screen until the new ones arrive. Data credits come from `/tile-sources/attribution` |
| Scenarios | `scenarios.html` | The scenario workspace: create a scenario, draw an area and set attribute values as rules, recompute, and compare scenario against baseline per indicator |
| Catalogue | `catalog.html` | Indicators and datasets with facets, a lineage graph per indicator and an impact check that shows what an attribute edit would recompute |
| Runs | `ops.html` | A health strip showing whether any worker is alive (from executor heartbeats) and how much work is in flight; a form to launch a run; the run list; a run's detail with an execution timeline per unit. The timeline predicts nothing: bars grow as units run, on an axis that widens when the run reaches its edge |

### 5.3 Level 2 — Components of the recomputation worker

The worker does one thing: it executes indicator units. The runner registry
binds the twelve shipped indicators to their compute functions, and nothing
else is dispatched to it. Ingestion pipelines and derivation jobs
(`reimagina.jobs`: overlaps, AOI summaries, observation rollups, raster zonal
statistics, derivations) run inside the operator CLI, in the operator's
process; the worker uses only `reimagina.jobs._lifecycle` to record a
`job_run` per unit.

![Component diagram](diagrams/c4_03_worker_components.svg)

Within a unit the work is split by kind. The spatial measures — geodesic
areas and lengths, counts by containment, nearest-amenity and network
distances through pgRouting — are SQL against PostGIS. The arithmetic on top
of them is a set of pure Python kernels composed in a Hamilton dataflow, each
result frame checked by a Pandera schema before it is written.

The components the delivery names, and where they live:

| Named component | Source location |
|---|---|
| Orchestration module | `packages/py/reimagina/orchestration/`, `packages/py/reimagina/jobs/_lifecycle.py`, `apps/worker/` |
| Data and indicator computation pipelines | `packages/py/reimagina/pipeline/`, `packages/py/reimagina/jobs/`, `packages/py/reimagina/indicators/`, `packages/py/reimagina/assets/`, `packages/py/reimagina/data/` |
| Scenario evaluation components | `packages/py/reimagina/scenarios/`, the routes in `apps/api/routers/scenarios.py`, and the scenario workspace in `apps/web/src/scenarios/` |

And the supporting packages:

| Package | Role |
|---|---|
| `reimagina.storage` | The repositories: every SQL statement the application issues, behind typed interfaces |
| `reimagina.db` | Connections and `bootstrap-db` (create the database, migrate to head) |
| `reimagina.tiles` | Tile-function names, layer manifest, PMTiles export |
| `reimagina.perf` | Benchmark harness and its latency budgets |
| `reimagina.core` | Reserved for cross-cutting utilities; empty in this release |
| `apps/api`, `apps/cli`, `apps/worker`, `apps/web` | The four applications of Figure 3 |

### 5.4 The data model, in brief

Two tables carry the canonical model, and the separation between them is the
single most load-bearing decision in the schema.

- **`curated_geo.feature`** — the canonical spatial anchor. Every block,
  AGEB, municipality, building, amenity and street segment is a feature. The
  routing graph itself lives in `curated_geo.road_node` and `road_edge`; its
  edges are published as `global:road` features so that they can be drawn.
- **`curated_geo.observation`** — measured or derived facts *about* a feature,
  held separately so that re-measuring something never rewrites the thing
  measured.

Indicator results, feature overlaps, scenario rules and overrides, and the
asset graph all hang off those two. The full schema is Work 3 of this
delivery.

### 5.5 Level 3 — Inside the orchestration module

The orchestration module is named among the components of the source-code
Work in Schedule A, and it is the part of the system whose design is least
visible from the outside, so it is drawn here rather than left at one box of
Figure 4.

![Orchestration components](diagrams/c4_06_orchestration_components.svg)

**The shape is a port with two implementations behind it.** Callers — the
API routers and the CLI — depend on `Orchestrator`, an interface of six
methods (`submit_run`, `get_run`, `list_enqueued_runs`, `forget_runs`,
`cancel_run`, `subscribe_progress`) carrying plain dataclasses. It names no
queue, no workflow handle and no run table, which is the property that makes
the engine replaceable: a signature mentioning any of those would be one a
different ops plane could not implement. `factory.py` builds either
implementation by name — `dbos`, the durable one and the only one a
deployment may select, or `sync`, the in-process reference. The worker and
`bootstrap-db` use the DBOS engine module directly, as does the CLI's
`evaluate-scenario` in its default, embedded mode, which deliberately owns an
executor.

That is not a hypothetical. The port exists because this repository ran two
ops planes side by side to choose between them, and the conformance suite
(`tests/conformance`) asserts eleven behaviours against both
implementations. An engine that cannot satisfy them is rejected by a test
rather than by an argument.

**Half the module survives an engine change and half does not**, and the
diagram separates them because the distinction governs where work belongs:

| Survives | Why |
|---|---|
| Impact engine | Deciding what an edit invalidates is a property of the indicator catalogue, not of how work is scheduled |
| Planner and graph | Resolving a dirty set into dependency-ordered units touches no queue and no run table |
| Wave scheduler | A pure function from unit statuses to "what may start now" — an engine that schedules DAGs natively deletes it; one that only provides durability keeps it |
| Run identity | What makes two submissions the same work is a question about the inputs, not the engine |
| Runners and coordinator | Which function computes which indicator, and the in-process executor the reference implementation uses |
| Progress framing | Turning run events into Server-Sent Events frames is transport, not engine |

| Replaceable | Why |
|---|---|
| Adapter, workflows and queues | Every durable-execution concept lives here: workflow ids, steps, checkpoints, queue registration |
| Run envelope | The run and unit rows in `jobs_runtime` that this engine keeps beside its own checkpoints |
| Reclaim and liveness | How a dead process's work is recovered is an engine question |

**Durability is a library, not a service.** Run state is checkpointed into the
same PostgreSQL as the domain data, under the library's own `dbos` schema.
There is no second system to deploy, and a checkpoint is transactional with
the data it describes. The cost is stated in AD5: the ops plane is tied to
Postgres.

**One rule shapes the workflow bodies, and it is not obvious.** A durable
workflow is re-executed from the top on recovery, and its recorded step
outputs are matched by position. So a workflow body that rebuilt its unit list
from the catalogue would not fail loudly if the catalogue changed mid-run — it
would hand one indicator's recorded result to another, with no exception and a
plausible-looking number. The dirty set is therefore resolved *before* the
workflow starts and passed in as an input. This was measured rather than
reasoned about; the experiment is spike 2 in
`docs/product/PHASE3_OPS_PLANE_DBOS_SPIKES.md`.

**A resubmission does not start a second run.** Submitting work that is
already outstanding returns the run already doing it, flagged so the caller
can tell. The identity is not the request, though: a scenario edit leaves the
request byte-identical while changing the values it resolves to, so the
identity folds in a content fingerprint of everything the planned units read
(`dedup.py`). Edit an override and the fingerprint moves, so the
resubmission is correctly a new run; click twice with nothing changed and it
is correctly one.

**Two different recoveries exist**, covering two different failures:

- A process that **comes back** reclaims its own interrupted workflows when it
  launches. That covers a restart, a redeploy, or an OOM kill with a
  supervisor behind it, and the library does it unaided.
- A process that **never comes back** — a destroyed host, a scaled-away
  replica — leaves work recorded against an identity nothing will ever claim.
  A live peer reclaims that, gated on a liveness table every executor writes.
  The gate is a correctness requirement: re-enqueuing work a running process
  is still executing would compute that indicator twice, so an executor must
  be silent for several heartbeat intervals before it is presumed dead, and
  the system refuses to start with a window narrower than that.

Both are proven against a real `SIGKILL` in `tests/recovery`
(`make test-recovery`).

## 6. Runtime View

### 6.1 End-to-end data flow

Figure 6 follows the data from its publishers to the pages, in three stages.

**Load**, run by the operator from the CLI. The ingestion pipelines in
`reimagina.pipeline` — `overture`, `overture_places`, `overture_divisions`,
`cdmx`, `inegi_marco`, `inegi_censo`, `points`, `osm_network` and `raster`,
the last fed by `worldpop` for population — fetch or read their source,
land and normalise it (§6.3), and write features, observations, the road
graph and raster registrations into `curated_geo`. Derivation jobs then read
and enrich that model: areas of interest and the feature overlaps that scope
them, raster zonal statistics, observation rollups, derivations and AOI
summaries.

**Compute**, as runs. A run is submitted from the API (`POST /jobs`, or
`POST /scenarios/{id}/recompute`) or from the CLI (`indicators`,
`scenario recompute`). The impact engine and planner turn it into
dependency-ordered units, which are enqueued on the DBOS `runs` and `units`
queues; the worker executes them and writes `curated_geo.indicator_result`,
baseline and scenario rows side by side.

**Serve.** `serving_geo` presents features and results as read views and
three tile functions. The API serves JSON, downloads, run progress, MVT and —
from the silver COGs on disk — raster tiles; Martin serves MVT when
configured. The four pages read both. Separately, `tiles export` writes a
PMTiles snapshot of a stable layer, which the nginx container serves to GIS
clients.

![End-to-end data flow](diagrams/c4_07_data_flow.svg)

### 6.2 Scenario recomputation

Figure 7 follows one scenario edit from the moment it is drawn to the moment
its effect is compared against the baseline.

![Scenario recomputation](diagrams/c4_04_scenario_dynamic.svg)

Authoring and computing are separate on purpose. A rule (step 1) records
intent only — an area, a feature type, an attribute and a value — and
nothing is computed until a recompute materialises it (step 2) into the
per-feature overrides the indicators read. Materialisation checks each
resolved value against its ceiling and refuses the whole set rather than
writing part of it.

The step that carries the most weight is the fourth. Because the asset graph
records which indicators read which *attribute*, an edit to `floors` marks
only the indicators that read `floors`. On the delivered catalogue of twelve
indicators that is exactly one; recomputing all twelve would be correct and
twelve times the work. Dependencies are followed the same way: an edit to
`num_levels` marks four — the three edificability indicators that read it
directly, and the livability score that reads it through them.

### 6.3 Ingestion

An ingestion run follows a medallion pattern. Bronze holds the source **as
received**, so the original bytes survive whatever happens later; silver holds
the normalised form — validated geometry in SRID 4326 for vectors, a
Cloud-Optimized GeoTIFF in its native CRS for rasters, consistent column
names. Only then does data reach `curated_geo`, where features and
observations are written under a source and a dataset version.

The bronze stage is about the *guarantee*, not the copy. The pipelines that
fetch from upstream land bronze: `cdmx`, `overture`, `overture_places` and
`raster`. Those that read the lake mirror — already an immutable copy — go
straight to silver (`inegi_marco`, `points`) or straight to the curated
tables (`inegi_censo`, which writes observations), rather than duplicating
gigabytes to buy a guarantee that already holds. `osm_network` and
`overture_divisions` load directly from their fetch.

## 7. Deployment View

The reference installation is one host. `infra/docker/docker-compose.yml`
runs the data services in containers — PostgreSQL with PostGIS and pgRouting,
pgbouncer, Martin, and an nginx serving PMTiles — and nothing else. The API,
the worker and the CLI are host processes started from the repository's
virtual environment; `make dev` starts the API, the worker and the Vite
development server together, and Vite serves the four pages and proxies
`/api` to the API. The repository ships no production web server: `npm run
build` produces a static bundle that any web server can host, given the same
`/api` proxy.

![Deployment diagram](diagrams/c4_05_deployment.svg)

| Process | Port | Started by |
|---|---|---|
| API (uvicorn) | 8001 (`API_PORT`) | `make dev` / `make dev-api` |
| Web dev server (Vite) | 5173 (`WEB_PORT`) | `make dev` / `make dev-web` |
| Worker | none | `make dev` / `make dev-worker` |
| pgbouncer | 5432 | `make stack-up` |
| PostgreSQL | 5433 on the host, 5432 in the container | `make stack-up` |
| Martin | 3000 | `make stack-up` |
| nginx (PMTiles) | 3001 | `make stack-up` |

Three aspects are architectural rather than operational:

- **Two tile paths.** Live vector tiles are primary, because a layer that must
  be filtered, scenario-aware or fast-changing cannot be served from a static
  snapshot. PMTiles exports exist only for stable reference layers, for GIS
  clients; the web pages never load them.
- **Two database paths.** The API's pooled connections go through pgbouncer
  in transaction mode (`REIMAGINA_APP_DSN`). Migrations, the worker, Martin
  and the API's ops-plane client connect directly (`DATABASE_URL_DIRECT`),
  because `LISTEN`/`NOTIFY` and the library's checkpoints cannot cross a
  transaction-mode pooler.
- **Plain HTTP.** No process terminates TLS; a networked deployment puts a
  reverse proxy with TLS and authentication in front (T1).

## 8. Crosscutting Concepts

### 8.1 The schemas and who reads them

| Schema | Holds (at `r051`) | Written by | Read by |
|---|---|---|---|
| `curated_geo` | The source of truth: 25 tables | CLI pipelines and jobs; the worker (`indicator_result`); the API's scenario routes (`scenario_edit`, `scenario_override`) | CLI, worker; the API for its catalogues (indicators, AOIs, sources and attribution) and for scenario authoring |
| `serving_geo` | The read contract: 23 views, one materialised view, three tile functions | Migrations define it; the CLI refreshes the materialised view after a load | API, Martin, PMTiles export |
| `explore` | Engineer-facing inspection: 11 views and one table, `layer_catalog` | Migrations and the CLI (`layer_catalog` rows) | CLI `explore` commands; the API's lineage endpoint (`asset_freshness_v`) |
| `app_core` | Projects and scenarios: 2 tables, the root of row-level security | CLI, API | CLI, API |
| `jobs_runtime` | Run envelope, units, plans, job records, executor heartbeats: 5 tables | API, CLI, worker | API, worker |
| `dbos` | The durable-execution library's own tables, created by the library rather than by migrations | The library | The library |

The rule that gives this its value is narrower than "the application reads
only `serving_geo`", and stated exactly it is this: **everything that
describes the city as the map and the downloads show it — features, their
attributes and indicator values — is read through `serving_geo`.** The API
reaches into `curated_geo` only for catalogue rows and to record scenario
intent, and into `explore` only for `asset_freshness_v`, which says how
fresh each asset on an indicator's lineage is. So the storage of features, observations and results can be
reshaped behind `serving_geo` without touching the API or the tile server,
and a contract test keeps domain logic from importing the `explore` module.

### 8.2 Spatial correctness

All 24 geometry columns are SRID 4326 and two-dimensional, enforced by the
column type rather than by convention. Every area, length and distance is
computed through a geography cast, so results are in metres rather than
square degrees. Ratios are clamped and divisors guarded, so a degenerate
geometry yields a null rather than a plausible-looking wrong number.

### 8.3 Scenario resolution

The effective value of an attribute under a scenario is resolved as
**override, then observation, then the base attribute** — the first present
wins. Results are written as new rows carrying the `scenario_id`, so baseline
and scenario coexist and can be read side by side. An indicator a scenario
has not recomputed falls back to its baseline value rather than showing a
hole in the map.

### 8.4 Security posture

Two things are worth stating precisely, because one is stronger than a
reader would assume and the other is weaker.

**Raster access is closed by default.** titiler's own `DatasetPathParams`
accepts any `url` and hands it to GDAL, so mounting it in-process would have
exposed arbitrary host-file reads — `/cog/info?url=file:///etc/passwd`.
`apps/api/cog_security.py` replaces it with a validator accepting only
`file://` URLs beneath the configured data root, resolving symlinks and
rejecting escapes.

**Row-level security is not a tenancy boundary.** The `tenant_isolation`
policies on `app_core.project`, `app_core.scenario` and
`curated_geo.area_of_interest` (migrations `r011` and `r048`) are enabled but
not forced, and their condition is permissive whenever
`app.current_project_id` is unset — and no application code sets it. The
migrations record that `FORCE ROW LEVEL SECURITY` and a non-owner
application role were deliberately deferred, and table owners bypass RLS in
PostgreSQL regardless. The policies are scaffolding for a tenancy model not
yet in force, and must not be read as one. See T6 in §11.

**Injection.** Every value that comes from a request is passed as a query
parameter, never interpolated. Statements are assembled dynamically in a few
places — `psycopg2.sql.Identifier` and `sql.SQL` composition for schema,
table and function names, and f-string fragments chosen from fixed,
code-controlled alternatives — but in each the varying part comes from the
code, not from a caller.

### 8.5 Testing

Tests are layered by what they can honestly prove. Continuous integration
(GitHub Actions) gates every change on lint (ruff, its formatter and mypy),
the unit and contract suites, the web application's type-check, build and
vitest suite, and the integration and conformance suites against a real
PostGIS. The rest are run by hand.

| Suite | Gated in CI | Proves |
|---|---|---|
| `unit` | yes | Pure logic, delegation, configuration, normalisation |
| `contract` | yes | Architectural boundaries — that `explore` is not imported by domain logic, that CI installs from the lockfile, that gating linters are pinned |
| web (`vitest`) | yes | The pages' derivation logic: legends, timelines, selection, scenario workspace state |
| `integration` | yes | Real PostgreSQL/PostGIS behaviour |
| `conformance` | yes | The orchestrator port contract: eleven behaviours, asserted against both implementations |
| `e2e` | no — `make test-e2e`, needs the API and the web dev server | Every page opened in a real browser |
| `performance` | no | Benchmark budgets and explain-plan shape |
| `workloads` | no — `make test-workloads` | How an ops plane schedules, over synthetic DAG topologies |
| `recovery` | no — `make test-recovery` | Crash recovery across a real process kill |

## 9. Architecture Decisions

| # | Decision | Rationale | Consequence |
|---|---|---|---|
| AD1 | Spatial measures are SQL against PostGIS; indicator arithmetic is small pure Python kernels in a Hamilton dataflow, with Pandera checks on every result | Keeps the geometry and its measurement in one place, and avoids moving large geometries into a process; keeps the arithmetic testable without a database and its output schema enforced | Indicator authors work in two layers; the database is the scaling bottleneck |
| AD2 | Schemas with distinct audiences (§8.1) | Lets the source of truth and the inspection surface evolve without breaking the map | More views to maintain |
| AD3 | Scenarios are overrides resolved at read time, not a mutated copy | Makes Q2 structural — there is no scenario column on the baseline tables to write to | Scenario reads cost a resolution step; a materialised projection would be faster but was not justified by measurement |
| AD4 | Recomputation is driven by a per-attribute dependency graph | An edit recomputes what it affects | The graph must be kept in step with what indicators actually read |
| AD5 | Durable execution in the same database rather than a separate engine | No second system to deploy or operate; checkpoints are transactional with the data | Ties the ops plane to Postgres, and to that library's major version — see T11 |
| AD6 | Live vector tiles primary, PMTiles for stable layers only | Scenario-aware and filtered layers cannot be static | Two tile paths to understand |

## 10. Quality Requirements

| Goal | How it is judged |
|---|---|
| Q1 Correctness | Unit and contract tests; integration tests against a real PostGIS; the conformance suite, which asserts the orchestrator port's eleven behaviours against both implementations; and an opt-in recovery suite that kills a running process |
| Q2 Baseline integrity | Structural: no scenario identifier exists on `feature` or `observation`, so a scenario cannot write to either |
| Q3 Inspectability | The `explore` schema, the CLI commands that read it, and the catalogue page's lineage and impact views |
| Q4 Efficient recomputation | An attribute edit dirties only the indicators declaring an edge to that attribute, and their dependents |
| Q5 Operability | Runs are listable, cancellable and re-runnable through the API and the runs page, and their progress streams live |

## 11. Risks and Technical Debt

Stated plainly, because a delivery that conceals them is worth less than one
that does not.

| # | Risk or debt | Assessment |
|---|---|---|
| T1 | **The API has no authentication.** It carries CORS and a request-id middleware only, so anyone who can reach the port can read all data and delete job history | Acceptable only while the deployment is private. Authentication, and TLS, are required before any networked deployment |
| T2 | ~~`maplibre-gl` 5.24.0 carried a critical sanitiser-bypass advisory~~ | **Resolved.** The web application now uses 6.18.0; no critical or high advisory reaches the shipped bundle |
| T3 | ~~No user-facing way to author a scenario edit~~ | **Resolved.** A scenario is created, edited with an area and attribute values, and recomputed through the API (`/scenarios`), the CLI (`scenario create`, `set-attribute`, `recompute`) and the scenario workspace. Edits are stored in `curated_geo.scenario_edit` and resolved into overrides on recompute; the baseline is never written |
| T4 | **Geometry cannot be edited by a scenario.** Overrides carry numeric and text values only | Scenario support is attribute-level; geometric what-ifs are future work |
| T5 | **A single database is the scaling bound.** Every indicator contends for the same PostGIS | Adequate at the delivered scale; revisit before a multi-city deployment |
| T6 | **Row-level security is inert.** The policies are permissive while `app.current_project_id` is unset, and nothing sets it; `FORCE ROW LEVEL SECURITY` and a non-owner role were deferred | Multi-tenant isolation is not enforced today. Required before more than one project shares a database |
| T7 | ~~**`storage_uri` records two different things**~~ | **Resolved** (#95). `data_source_catalog.storage_uri` now records where the data came from for every source; the local copy is not stored there |
| T8 | **Geometry validity is enforced only in the pipeline.** `silver.clean_vector` repairs and validates; nothing asserts the invariant at the database boundary | The failure mode is a loud GEOS `TopologyException`, not a silently wrong number, which is why this ranks where it does. A per-row `ST_IsValid` CHECK is the wrong fix, re-validating every polygon on every write; an `explore` view counting invalid geometries per type would fit the existing inspection contract |
| T9 | **`ST_Intersection` is evaluated three times per candidate pair** in the overlap query — in the measure, the ratio and the `WHERE` | A single evaluation via `LATERAL` is semantically identical, but no measurement supports a rewrite yet. `docs/architecture/PERFORMANCE.md` benchmarks this path; change it only against those numbers |
| T10 | **Several database stacks are in play.** The Compose stack and CI run PostgreSQL 16 / PostGIS 3.4 / pgRouting 3.6.1; the performance baselines were measured on PostgreSQL 17 / PostGIS 3.4; the development database this document was checked against runs PostgreSQL 17.10 / PostGIS 3.5.6 / pgRouting 3.6.3 | GEOS behaviour on invalid input changes between PostGIS minors, which is the layer T8 concerns. The stacks should be made to agree and the baselines re-measured |
| T11 | **The ops plane is coupled to one library's major version.** The durable-execution library's 3.0 changed its stored schema for workflow inputs and outputs: version 3 reads version 2's workflows, version 2 cannot read version 3's | The upgrade performed in this delivery cost one configuration line, and the port exists precisely so the engine can be replaced. But the migration is one-way, so a deployment must drain its in-flight runs before upgrading, and a rollback is not available |

## 12. Glossary

| Term | Definition |
|---|---|
| AGEB | *Área Geoestadística Básica* — INEGI's basic statistical area, between a block and a municipality in size |
| AOI | Area of interest: a polygon over which indicator values are aggregated for display |
| Asset | Anything the platform produces whose freshness is tracked — a layer, an indicator, a scenario override |
| Baseline | The territory as recorded, before any scenario edit |
| Bronze / silver | Ingestion stages: the source file as received, and its normalised form |
| COG | Cloud-Optimized GeoTIFF: a GeoTIFF laid out so that tiles can be read without reading the whole file |
| Container | *C4 sense*: an application or data store that must be running for the system to work. Not a Docker container |
| DBOS | The durable-execution library the ops plane is built on; it checkpoints workflows into PostgreSQL |
| Feature | The canonical spatial entity — a block, AGEB, municipality, building, amenity or street segment |
| Hamilton | The dataflow library that composes the indicator kernels into a graph of pure functions |
| Heartbeat | The periodic row each executor writes to prove it is alive; reclaim is gated on its absence |
| Indicator | A value computed from feature attributes and observations, at a defined grain |
| Materialisation | Resolving a scenario's rules into per-feature override rows, on recompute |
| Medallion | The bronze-then-silver staging pattern of ingestion |
| MVT | Mapbox Vector Tile, the wire format for live vector tiles |
| Observation | A measured or derived fact about a feature, held separately from the feature itself |
| Override | A scenario's replacement value for one attribute of one feature |
| pgbouncer | The connection pooler between the API and PostgreSQL, in transaction mode |
| PMTiles | A single-file archive of map tiles, used for snapshot exports |
| Rule | A scenario edit as authored: an area, a feature type, an attribute and a value |
| Scenario | A named set of hypothetical edits evaluated against the baseline |
| SRID 4326 | WGS 84 geographic coordinates, the CRS of every vector geometry |
| SSE | Server-Sent Events, the stream a run's progress is delivered on |

## References

1. arc42 — template for software architecture documentation. <https://arc42.org>
2. Simon Brown, *The C4 model for visualising software architecture*. <https://c4model.com>
3. *Database and API reference* — Schedule A, Work 3 of this delivery.
4. *Technical documentation* — Schedule A, Work 4 of this delivery.
5. *Third-party dependencies and licences* — `docs/THIRD_PARTY_LICENSES.md`.
6. *Delivery manifest* — `docs/delivery/MANIFEST.md`.
