---
title: "Software and systems architecture map"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — Schedule A, Work 1"
author: "Rossano Schifanella"
date: "8 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Schedule A, Work 1 — Software and systems architecture map |
| **Status** | Draft for review |
| **Audience** | Technical reviewers, maintainers and operators of the platform |

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | 2026-11-08 | Rossano Schifanella | First complete draft |
| 0.2 | 2026-11-08 | Rossano Schifanella | Indicator count corrected to eleven, measured against the running system |
| 0.3 | 2026-11-09 | Rossano Schifanella | Orchestration module documented (§5.4, Figure 5); current dependency set |

### About this document

This document follows **arc42**, the open template for software architecture
documentation, whose twelve sections each hold one kind of architectural
information. Sections that carry little content for this system are kept
short rather than padded, which arc42 explicitly permits.

The diagrams follow the **C4 model**. Each has a title naming its type and
scope, each element states its type and — for containers and components —
its technology, and every relationship is a labelled, one-way line that names
the protocol where processes communicate. The notation key in §3.1 applies to
every figure.

Everything factual here was derived from the delivered system: component
responsibilities from the packages themselves, schema and endpoint counts
from a migrated database and the application's own OpenAPI document, runtime
topology from the deployment configuration. **Where this document and the
code disagree, the code is correct and this document is the defect.**

---

## 1. Introduction and Goals

Reimagina is an urban-planning data platform. It ingests public and
proprietary geospatial data about a territory, normalises it into one
canonical model, computes urban indicators over it, and serves the result to
a web map. On top of that baseline it evaluates **scenarios** — hypothetical
edits whose effect on the indicators is computed and compared against the
city as it stands.

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
| C1 | Source data is mirrored in a private Azure data lake | Ingestion depends on credentials to that lake; without it the platform installs and runs but cannot load the territory |
| C2 | Spatial work is done in PostGIS, not in application memory | Indicators are expressed as SQL; the database is the computational centre, not a passive store |
| C3 | Network distance requires pgRouting | pgRouting is a hard dependency, and it is not a *trusted* Postgres extension, so installation needs a superuser |
| C4 | One coordinate reference system throughout | Every geometry column is SRID 4326, enforced by the column type |
| C5 | The platform is delivered as source, to be run by the recipient | Installation must be reproducible from the repository by a competent reader |

## 3. Context and Scope

### 3.1 Notation key

![Notation key](diagrams/c4_00_legend.svg)

*Figure 1 — Notation key for all diagrams in this document.*

### 3.2 System context

![System context](diagrams/c4_01_system_context.svg)

*Figure 2 — System Context diagram for Reimagina.*

Reimagina sits between the open-data publishers whose datasets describe the
territory and the people who reason about it. It does not publish data
upstream and it does not write to the data lake: the lake is a read-only
mirror, which is what lets an ingestion be repeated without risk to the
source.

### 3.3 External interfaces

| Interface | Direction | Protocol | Purpose |
|---|---|---|---|
| Azure data lake | Inbound | HTTPS | Source datasets: INEGI marco geoestadístico 2024, INEGI censo 2020, SIGCDMX/EXE amenities, Overture, WorldPop |
| Web client | Outbound | JSON over HTTPS | Features, indicators, AOI summaries, downloads, job control |
| Vector tiles | Outbound | MVT over HTTPS | Live map rendering |
| Raster tiles | Outbound | HTTPS | Cloud-Optimized GeoTIFF tiles, served in-process by the API |

## 4. Solution Strategy

Five decisions shape everything else. §9 records why each was taken.

| Decision | Serving |
|---|---|
| Keep the spatial computation in PostGIS and express indicators as SQL | Q1, C2 |
| Separate the source of truth from what the application may read, using distinct schemas | Q1, Q3 |
| Record scenario edits as overrides resolved at read time, never as mutations of the baseline | Q2 |
| Drive recomputation from a dependency graph over attributes | Q4 |
| Run the ops plane as durable execution inside the same database | Q5 |

## 5. Building Block View

### 5.1 Level 1 — Containers

![Container diagram](diagrams/c4_02_containers.svg)

*Figure 3 — Container diagram for Reimagina.*

Two details in this figure are deliberate rather than incidental. The tile
server reads the same serving views the API reads, so a map and a table can
never disagree about what the city contains. And the worker talks to the
database directly rather than through the pooler, because its progress
streams use `LISTEN`/`NOTIFY`, which cannot cross a transaction-mode pooler.

### 5.2 Level 2 — Components of the recomputation worker

![Component diagram](diagrams/c4_03_worker_components.svg)

*Figure 4 — Component diagram for the Recomputation worker container.*

This container is shown in detail because it holds the components the
delivery names. Their locations in the source tree:

| Named component | Source location |
|---|---|
| Orchestration module | `packages/py/reimagina/orchestration/`, `packages/py/reimagina/jobs/`, `apps/worker/` |
| Data and indicator computation pipelines | `packages/py/reimagina/pipeline/`, `packages/py/reimagina/indicators/`, `packages/py/reimagina/assets/`, `packages/py/reimagina/data/` |
| Scenario evaluation components | `packages/py/reimagina/scenarios/`, and the scenario-aware read paths in `apps/api/routers/` |

### 5.3 The data model, in brief

Two tables carry the canonical model, and the separation between them is the
single most load-bearing decision in the schema.

- **`curated_geo.feature`** — the canonical spatial anchor. Every lot, block,
  AGEB, building, amenity and road node is a feature.
- **`curated_geo.observation`** — measured or derived facts *about* a feature,
  held separately so that re-measuring something never rewrites the thing
  measured.

Indicator results, feature overlaps and the asset graph all hang off those
two. The full schema is Work 3 of this delivery.

### 5.4 Level 3 — Inside the orchestration module

![Orchestration components](diagrams/c4_06_orchestration_components.svg)

*Figure 5 — Component diagram for the orchestration module.*

Schedule A names the orchestration module as a deliverable in its own right,
and it is the part of the system whose design is least visible from the
outside, so it is drawn here rather than left at the one box of Figure 4.

**The shape is a port with one adapter behind it.** Callers — the API, the
CLI — depend on `Orchestrator`, an interface of five methods carrying plain
dataclasses. It names no queue, no workflow handle and no run table, which is
the property that makes the engine replaceable: a signature mentioning any of
those would be one a different ops plane could not implement. `factory.py` is
the single place an implementation is named, so swapping engines edits one
file rather than every call site.

That is not a hypothetical. The port exists because this repository ran two
ops planes side by side to choose between them, and the conformance suite
(`tests/conformance`) asserts the same seventeen behaviours against each
implementation. An engine that cannot satisfy them is rejected by a test
rather than by an argument.

**Half the module survives an engine change and half does not**, and the
diagram separates them because the distinction governs where work belongs:

| Survives | Why |
|---|---|
| Impact engine | Deciding what an edit invalidates is a property of the indicator catalogue, not of how work is scheduled |
| Planner and graph | Resolving a dirty set into dependency-ordered units touches no queue and no run table |
| Wave scheduler | A pure function from unit statuses to "what may start now" — an engine that schedules DAGs natively deletes it; one that only provides durability keeps it |

| Replaceable | Why |
|---|---|
| Adapter and workflow definitions | Every durable-execution concept lives here: workflow ids, steps, checkpoints, queue registration |
| Reclaim and liveness | How a dead process's work is recovered is an engine question |

**Durability is a library, not a service.** Run state is checkpointed into the
same PostgreSQL as the domain data, under its own `dbos` schema. There is no
second system to deploy, and a checkpoint is transactional with the data it
describes. The cost is stated in AD5: the ops plane is tied to Postgres.

**One rule shapes the workflow bodies, and it is not obvious.** A durable
workflow is re-executed from the top on recovery, and its recorded step
outputs are matched by position. So a workflow body that rebuilt its unit list
from the catalogue would not fail loudly if the catalogue changed mid-run — it
would hand one indicator's recorded result to another, with no exception and a
plausible-looking number. The dirty set is therefore resolved *before* the
workflow starts and passed in as an input. This was measured rather than
reasoned about; the experiment is in the project's spike report.

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

Both are proven against a real `SIGKILL` in `tests/recovery`, and the
transcripts are in the capability-proof annex.

## 6. Runtime View

### 6.1 Scenario recomputation

![Scenario recomputation](diagrams/c4_04_scenario_dynamic.svg)

*Figure 6 — Dynamic diagram: recomputing indicators after a scenario edit.*

The step that carries the most weight is the third. Because the asset graph
records which indicators read which *attribute*, an edit to `floors` marks
only the indicators that read `floors`. On the delivered catalogue of eleven
indicators that is exactly one, measured against the running system; recomputing
all eleven would be correct and eleven times the work. Two hops are resolved the
same way: an edit to `num_levels` marks four, because two of them read it only
through a composite.

### 6.2 Ingestion

An ingestion run follows a medallion pattern. Bronze holds the source **as
received**, so the original bytes survive whatever happens later; silver holds
the normalised form — one CRS, validated geometry, consistent column names.
Only then does data reach `curated_geo`, where features and observations are
written under a source and a dataset version.

The bronze stage is about the *guarantee*, not the copy. Where the registered
source is already an immutable mirror — as the Azure data lake is — the
pipeline lands straight to silver rather than duplicating gigabytes to buy a
guarantee that already holds. Two of the three vector pipelines do this and
record why; the one that fetches from upstream portals directly does land
bronze.

## 7. Deployment View

![Deployment diagram](diagrams/c4_05_deployment.svg)

*Figure 7 — Deployment diagram for a single-host installation.*

The local stack is defined in `infra/docker/docker-compose.yml`. Two aspects
are architectural rather than operational:

- **Two tile paths.** Live vector tiles are primary, because a layer that must
  be filtered, scenario-aware or fast-changing cannot be served from a static
  snapshot. PMTiles exports exist only for stable reference layers.
- **Two database paths.** The API reads through the pooler; migrations, the
  ops plane and anything using `LISTEN`/`NOTIFY` connect directly.

## 8. Crosscutting Concepts

### 8.1 The three-schema rule

| Schema | Role | Who may read it |
|---|---|---|
| `curated_geo` | Source of truth | Ingestion and computation only |
| `serving_geo` | The application read contract | API, worker, tile server |
| `explore` | Engineer-facing inspection | Humans, and the CLI |

The rule that gives this its value is the last column. Because no application
code reads `explore`, those views can be improved freely without risking the
product; and because no application code reads `curated_geo` directly, the
source of truth can be reshaped behind `serving_geo`.

### 8.2 Spatial correctness

All 23 geometry columns are SRID 4326 and two-dimensional, enforced by the
column type rather than by convention. Every area, length and distance is
computed through a geography cast, so results are in metres rather than
square degrees. Ratios are clamped and divisors guarded, so a degenerate
geometry yields a null rather than a plausible-looking wrong number.

### 8.3 Scenario resolution

The effective value of an attribute under a scenario is resolved in SQL as
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

**Row-level security is not a tenancy boundary.** Policies exist from
migration `r011`, but their condition is permissive whenever
`app.current_project_id` is unset, and no application code sets it. The
migration itself records that `FORCE ROW LEVEL SECURITY` and a non-owner
application role were deliberately deferred — and table owners bypass RLS in
PostgreSQL regardless. The policies are scaffolding for a tenancy model not
yet in force, and must not be read as one. See T6 in §11.

**Injection.** SQL is parameterised throughout; the only dynamic construction
is a `sql.Identifier` composition and the performance harness's own benchmark
statements.

### 8.5 Testing

Tests are layered by what they can honestly prove. Four suites gate CI;
three are hand-run.

| Suite | Gated | Proves |
|---|---|---|
| `unit` | yes | Pure logic, delegation, configuration, normalisation |
| `contract` | yes | Architectural boundaries — that `explore` is not imported by domain logic, that CI installs from the lockfile, that gating linters are pinned |
| `integration` | yes | Real PostgreSQL/PostGIS behaviour |
| `conformance` | yes | The orchestrator port contract, asserted against every implementation |
| `performance` | no | Benchmark budgets and explain-plan shape |
| `workloads` | no | How an ops plane schedules, over synthetic DAG topologies |
| `recovery` | no | Crash recovery across a real process kill; opt-in |

## 9. Architecture Decisions

| # | Decision | Rationale | Consequence |
|---|---|---|---|
| AD1 | Indicators are SQL against PostGIS, not application code | Keeps the data and the computation in one place; avoids moving large geometries into a process | Indicator authors must know SQL; the database is the scaling bottleneck |
| AD2 | Three schemas with distinct audiences | Lets the source of truth and the inspection surface evolve without breaking the application | More views to maintain |
| AD3 | Scenarios are overrides resolved at read time, not a mutated copy | Makes Q2 structural — there is no scenario column on the baseline tables to write to | Scenario reads cost a resolution step; a materialised projection would be faster but was not justified by measurement |
| AD4 | Recomputation is driven by a per-attribute dependency graph | An edit recomputes what it affects | The graph must be kept in step with what indicators actually read |
| AD5 | Durable execution in the same database rather than a separate engine | No second system to deploy or operate; checkpoints are transactional with the data | Ties the ops plane to Postgres, and to that library's major version — see T11 |
| AD6 | Live vector tiles primary, PMTiles for stable layers only | Scenario-aware and filtered layers cannot be static | Two tile paths to understand |

## 10. Quality Requirements

| Goal | How it is judged |
|---|---|
| Q1 Correctness | 534 unit and contract tests, 322 integration tests against a real PostGIS, 17 conformance tests of the orchestrator port contract, and an opt-in recovery suite that kills a running process |
| Q2 Baseline integrity | Structural: no scenario identifier exists on `feature` or `observation`, so a scenario cannot write to either |
| Q3 Inspectability | The `explore` schema and the CLI commands that read it |
| Q4 Efficient recomputation | An attribute edit dirties only the indicators declaring an edge to that attribute |
| Q5 Operability | Runs are listable, cancellable and re-runnable through the API and the run dashboard |

## 11. Risks and Technical Debt

Stated plainly, because a delivery that conceals them is worth less than one
that does not.

| # | Risk or debt | Assessment |
|---|---|---|
| T1 | **The API has no authentication.** It carries CORS and a request-id middleware only, so anyone who can reach the port can read all data and delete job history | Acceptable only while the deployment is private. Authentication is required before any networked deployment |
| T2 | ~~`maplibre-gl` 5.24.0 carried a critical sanitiser-bypass advisory~~ | **Resolved.** Upgraded to 6.17.0; no critical or high advisory now reaches the shipped bundle |
| T3 | **No user-facing way to author a scenario edit.** The storage, resolution, graph propagation and recomputation all work and are tested, but no API route or CLI command writes an override | The scenario engine is complete; the entry point into it is not |
| T4 | **Geometry cannot be edited by a scenario.** Overrides carry numeric and text values only | Scenario support is attribute-level; geometric what-ifs are future work |
| T5 | **A single database is the scaling bound.** Every indicator contends for the same PostGIS | Adequate at the delivered scale; revisit before a multi-city deployment |
| T6 | **Row-level security is inert.** The `r011` policies are permissive while `app.current_project_id` is unset, and nothing sets it; `FORCE ROW LEVEL SECURITY` and a non-owner role were deferred | Multi-tenant isolation is not enforced today. Required before more than one project shares a database |
| T7 | **`storage_uri` records two different things.** `points` and `inegi_marco` register the upstream path; `cdmx` overwrites it with the local bronze path | Both are traceable, but a provenance column that answers "where did this come from" for two sources and "where is my copy" for a third is not provenance. Needs a decision, not a patch |
| T8 | **Geometry validity is enforced only in the pipeline.** `silver.clean_vector` repairs and validates; nothing asserts the invariant at the database boundary | The failure mode is a loud GEOS `TopologyException`, not a silently wrong number, which is why this ranks where it does. A per-row `ST_IsValid` CHECK is the wrong fix on ~1.5M polygons; an `explore` view counting invalid geometries per type would fit the existing inspection contract |
| T9 | **`ST_Intersection` is evaluated three times per candidate pair** in the overlap query — in the measure, the ratio and the `WHERE` | A single evaluation via `LATERAL` is semantically identical, but no measurement supports a rewrite yet. `PERFORMANCE.md` benchmarks this path; change it only against those numbers |
| T11 | **The ops plane is coupled to one library's major version.** The durable-execution library's 3.0 changed its stored schema for workflow inputs and outputs: version 3 reads version 2's workflows, version 2 cannot read version 3's | The upgrade performed in this delivery cost one configuration line, and the port exists precisely so the engine can be replaced. But the migration is one-way, so a deployment must drain its in-flight runs before upgrading, and a rollback is not available |
| T10 | **Three database stacks are in play.** Performance baselines were measured on PostgreSQL 17 / PostGIS 3.4, CI runs PostgreSQL 16 / PostGIS 3.4 / pgRouting 3.6.1 | GEOS behaviour on invalid input changes between PostGIS minors, which is the layer T8 concerns. The stacks should be made to agree and the baselines re-measured |

## 12. Glossary

| Term | Definition |
|---|---|
| AGEB | *Área Geoestadística Básica* — INEGI's basic statistical area, between a block and a municipality in size |
| AOI | Area of interest: a polygon over which indicator values are aggregated for display |
| Asset | Anything the platform produces whose freshness is tracked — a layer, an indicator, a scenario override |
| Baseline | The territory as recorded, before any scenario edit |
| Bronze / silver | Ingestion stages: the source file as received, and its normalised form |
| Container | *C4 sense*: an application or data store that must be running for the system to work. Not a Docker container |
| Feature | The canonical spatial entity — a lot, block, AGEB, building, amenity or road node |
| Indicator | A value computed from feature attributes and observations, at a defined grain |
| MVT | Mapbox Vector Tile, the wire format for live vector tiles |
| Observation | A measured or derived fact about a feature, held separately from the feature itself |
| Override | A scenario's replacement value for one attribute of one feature |
| Scenario | A named set of hypothetical edits evaluated against the baseline |
| SRID 4326 | WGS 84 geographic coordinates, the single CRS used throughout |

## References

1. arc42 — template for software architecture documentation. <https://arc42.org>
2. Simon Brown, *The C4 model for visualising software architecture*. <https://c4model.com>
3. *Database and API reference* — Schedule A, Work 3 of this delivery.
4. *Technical documentation* — Schedule A, Work 4 of this delivery.
5. *Third-party dependencies and licences* — `docs/THIRD_PARTY_LICENSES.md`.
