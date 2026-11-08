# Software and systems architecture map

*Reimagina — Schedule A, Work 1.*

This document describes the architecture of the delivered platform: its main
components, how data moves between them, and the external systems it depends
on. It is written for a reader who has not seen the source tree.

Every figure in it was derived from the delivered system rather than drawn
from memory — component names and responsibilities come from the packages
themselves, counts from the migrated database and the application's own
OpenAPI document. Where this document and the code disagree, the code is
right and this document is a defect.

---

## 1. What the platform is

Reimagina is an urban-planning data platform. It ingests public and
proprietary geospatial data about a territory, normalises it into a single
canonical model, computes indicators over it, and serves the result to a web
map. On top of that baseline it supports **scenarios**: hypothetical edits
whose effect on the indicators can be computed and compared against the city
as it is.

Three properties shape the whole design:

- **The baseline is never modified by a scenario.** A scenario contributes
  override rows; the canonical features and observations are untouched. This
  is structural, not a convention — there is no scenario column on either
  table to write to.
- **One schema is the application's only read path.** Everything the API, the
  worker and the tile server read comes from `serving_geo`. The source of
  truth, `curated_geo`, is not read directly by application code.
- **Recomputation is driven by a dependency graph, not by a schedule.** An
  edit publishes a version of one asset; the graph determines precisely which
  indicators are now stale, and only those are recomputed.

## 2. Component map

Ingestion and computation:

![Ingest and compute](diagrams/01a_ingest_and_compute.svg)

Serving:

![Serving](diagrams/01b_serving.svg)

### Backend packages

| Package | Responsibility |
|---|---|
| `reimagina.pipeline` | Bronze, silver and curated ingestion flows |
| `reimagina.indicators` | The indicator catalog and its runners |
| `reimagina.assets` | The asset graph: one dependency graph over everything the platform produces |
| `reimagina.scenarios` | Scenario overrides and effective-value resolution |
| `reimagina.orchestration` | Job orchestration — the domain core |
| `reimagina.jobs` | Job definitions executed by the worker |
| `reimagina.storage` | Storage backend and connection routing for PostGIS |
| `reimagina.data` | Canonical data-layer modules for curated and serving data |
| `reimagina.tiles` | Tile manifest, snapshot export and source resolution |
| `reimagina.db` | Database helpers and migration utilities |
| `reimagina.core` | Cross-cutting utilities, settings and shared types |
| `reimagina.perf` | Benchmark and observability helpers |

### Applications

| Application | Responsibility |
|---|---|
| `apps.api` | FastAPI read-side API — 48 operations across 44 paths, with an in-process titiler for raster tiles |
| `apps.worker` | Ops-plane worker draining the `compute` and `control` queues |
| `apps.web` | MapLibre GL web client (Vite, TypeScript) — map, indicator catalog, run dashboard |
| `apps.cli` | Operator CLI for ingest, inspection and publishing |

### Mapping to the delivered source

The source code deliverable names three components. They are located as
follows.

| Named component | Where it lives |
|---|---|
| Orchestration module | `packages/py/reimagina/orchestration/`, `packages/py/reimagina/jobs/`, `apps/worker/` |
| Data and indicator computation pipelines | `packages/py/reimagina/pipeline/`, `packages/py/reimagina/indicators/`, `packages/py/reimagina/assets/`, `packages/py/reimagina/data/` |
| Scenario evaluation components | `packages/py/reimagina/scenarios/`, and the scenario-aware read paths in `apps/api/routers/` |

## 3. Data flow

![Data flow](diagrams/02_dataflow.svg)

Ingestion follows a medallion pattern. An upstream file is copied **as
received** into bronze, so the original bytes remain available whatever
happens later. Silver holds the normalised form: a single CRS, validated
geometry, consistent column names. Only then does data reach the canonical
model.

Two tables carry that model:

- **`curated_geo.feature`** — the canonical spatial anchor. Every lot,
  block, AGEB, building, amenity and road node is a feature.
- **`curated_geo.observation`** — measured or derived facts about a feature,
  kept separate so that re-measuring something does not rewrite the thing
  measured.

Indicator results, feature overlaps and the asset graph all hang off those
two. `serving_geo` then exposes the subset the application is allowed to see,
and `explore` exposes a parallel set of views built for engineers reading SQL
directly — deliberately not depended on by application code, so that
improving them can never break the product.

All 23 geometry columns are SRID 4326 and two-dimensional, enforced by the
column type rather than by convention. Area, length and distance are computed
in a geography cast, so results are in metres rather than square degrees.

## 4. Scenario evaluation

![Scenario recomputation](diagrams/03_scenario_recompute.svg)

A scenario edit is a row in `curated_geo.scenario_override`, keyed by
scenario, feature and attribute. Writing one publishes a new version of the
asset representing that attribute for that scenario.

That version is what drives recomputation. The asset graph records which
indicators read which attributes, so publishing an edit to `floors` marks
only the indicators that actually read `floors` as stale. The alternative —
recomputing everything on any edit — was measured and rejected: a single
override currently dirties one indicator out of thirteen.

During computation the effective value of an attribute is resolved in SQL as
**override, then observation, then the base attribute** — the first one
present wins. Results are written as new `indicator_result` rows carrying the
`scenario_id`, so baseline and scenario results coexist and can be read side
by side. A scenario that has recomputed only some indicators falls back to
baseline values for the rest, rather than showing holes.

## 5. External integrations

| System | Role |
|---|---|
| Azure data lake (`cfcetlsadls/raw`) | Upstream mirror of INEGI marco geoestadístico 2024, INEGI censo 2020, SIGCDMX/EXE amenities, Overture and WorldPop. Read-only; the platform never writes here |
| PostgreSQL 16 + PostGIS + pgRouting | The database. pgRouting backs network-distance indicators |
| pgbouncer | Connection pooling for the API. Migrations and the ops plane deliberately bypass it — both need a real server connection |
| Martin | Live vector tile server, reading `serving_geo` directly |
| titiler | Raster tiles, run in-process inside the API rather than as a separate service |
| MapLibre GL | The map client |
| DBOS | Durable execution for the ops plane. Checkpoints live in the same database, so there is no second engine to operate |

## 6. Runtime topology

The local stack is defined in `infra/docker/docker-compose.yml`: Postgres
with PostGIS and pgRouting, pgbouncer, Martin, and a static host for PMTiles
snapshots. The API and worker run as Python processes against them.

Two tile paths exist deliberately. Live vector tiles are the primary path,
because a layer that must be filtered, scenario-aware or fast-changing cannot
be served from a static snapshot. PMTiles snapshots are an export for stable
reference layers only.

Two database connection paths exist for the same kind of reason. The API
reads through pgbouncer; migrations, the ops plane and anything using
LISTEN/NOTIFY connect directly, because those cannot cross a transaction-mode
pooler.
