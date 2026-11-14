# Reimagina Phase 2 — Design Review

## What we're building

Reimagina is an urban-planning platform. Phase 2 adds the ability for users to draft hypothetical changes to a real territory and see how indicators (housing density, population, accessibility, COS, CUS, total housing units, etc.) change as a result. The expected user journey:

1. Open a project; the territory is shown as it is today — the **baseline**.
2. Create a **scenario** to explore a hypothesis ("what if we add 200 new buildings in this block?", "what if we rezone this land?").
3. Inside the scenario, sketch edits on the map: move a lot, change a building's height, add a new wall. Every edit is recorded as an **intervention**.
4. Click **Generate**. The platform computes the resulting state of the territory and recomputes indicators on top of it.
5. Open the **compare panel** to see the numbers side by side — baseline vs scenario, or scenario vs scenario.

The design rests on three rules that make all of this clean:

- **The intervention log is the source of truth.** Every edit is one row. Replaying the log always produces the same result. The platform never overwrites an Element in place.
- **A separate read model holds the resolved state.** A background worker computes a per-scenario projection that holds the final geometry, attributes and indicator values for every Element in the scenario. The compare panel reads only this projection — fast, predictable, comparable across scenarios.
- **AoI overlap is recomputed per scenario.** Indicator aggregates over Areas of Analysis depend on which Elements fall inside which AoI. Scenario edits change geometry, so the worker rebuilds the overlap per scenario.

The combination of these three is a canonical pattern: **CQRS-lite event sourcing** — the same shape that powers Datomic, EventStore, Kafka-Streams projections, and modern versioned databases like Dolt. Nothing in this design is invented for this project.

---

## Vocabulary

The brief defines five core terms used throughout this document.

- **Element** — a spatial entity in the territory (building, lot, block, …). In the schema this is `element` / `element_id`.
- **Area of Analysis (AoI)** — a polygon over which indicators are aggregated. The user defines them; indicators always come back at this granularity for display.
- **Scenario** — a named, possibly-forked branch containing hypothetical interventions on the baseline. Two siblings can diverge; both can be compared back to the baseline or to each other.
- **Intervention** — a user-authored change (add a new Element, modify attributes of an existing one, delete one) applied within one scenario. The intervention is the atomic edit; the scenario is the bundle of edits.
- **Indicator** — a value computed from one or more Element attributes (e.g. building density from footprint areas + block size). Computed per Element, then aggregated over an AoI for display.

The tables introduced below are `element`, `intervention`, `element_state`, `element_aoi_state`, `element_aoi_baseline`, `scenario`, `scenario_generation`. `area_of_interest` is the AoI table referenced by §4.3 of the brief. `job_run` is the generic worker-job table that supports any background job kind.

---

## Data model

The model has a clean three-layer shape:

- **Catalog layer** (`element`, `area_of_interest`, `project`) — the real-world territorial data plus its scenario-introduced extensions, plus the user-defined analysis polygons.
- **Write layer** (`scenario`, `intervention`) — the branches and their authored event logs.
- **Projection layer** (`element_state`, `element_aoi_state`, `element_aoi_baseline`, `scenario_generation`) — the resolved per-scenario read model and supporting tables, computed by the worker.

Plus the `job_run` infrastructure table — referenced by `scenario` (in-flight generation) and `scenario_generation` (history of runs).

**Data flows top to bottom.** Users author into the WRITE layer against a CATALOG of Elements and AoIs. The worker reads both and produces the PROJECTION read model that the compare panel queries.

**Two recursive relationships.** Scenarios fork from scenarios (`scenario.parent_scenario_id`); Elements have parent Elements (`element.parent_element_id`).

### What each table is

Each table below has a paragraph explaining what it stores, why it exists, and how it relates to the rest of the model.

#### Catalog layer

**`project`** — The top-level container in the multi-tenant model. Each pilot deployment (Condesa, Tlalpan, future cities) is one project row. All scenarios, AoIs, and scenario-introduced Elements belong to a specific project. The `baseline_dataset_version` field pins this project to a specific snapshot of the source data (e.g. "INEGI 2024"); when source data is refreshed, projects roll forward explicitly rather than drifting silently.

*Example row.*

| column | value |
|---|---|
| `id` | `P1` |
| `name` | `"Condesa pilot"` |
| `baseline_dataset_version` | `"inegi-2024-q3"` |
| `created_at` | `2026-04-01` |

Reads as: *"This is the Condesa pilot project, working against the INEGI Q3 2024 snapshot."*

**`area_of_interest`** — The user-defined polygons over which indicators are aggregated and reported. An AoI is the granularity at which numbers appear in the compare panel: "in this AoI, building density goes from 0.4 to 0.5 in scenario X." Each AoI belongs to one project, and a project can have many (typically one per neighbourhood plus a project-wide one). The compare panel can show two different AoIs side by side, but each panel's numbers always come from one AoI.

*Example row.*

| column | value |
|---|---|
| `id` | `AOI-A` |
| `project_id` | `P1` |
| `name` | `"Av. Amsterdam corridor"` |
| `geometry` | MultiPolygon covering the avenue |

Reads as: *"In the Condesa project, this is the analysis polygon for the Avenida Amsterdam corridor — all indicators reported for this AoI will be aggregated over the Elements that fall inside it."*

**`element`** — Every Element ever known to the platform: the real-world Elements ingested from sources (INEGI, Overture, etc.) *and* the hypothetical ones introduced by user interventions during scenario authoring. An `origin` column distinguishes the two: `'baseline'` for ingested data, `'scenario:<uuid>'` for ones added inside a specific scenario. The geometry and attributes columns are populated **only for baseline rows** — they are the canonical territorial data and the source the baseline `element_state` is seeded from. For scenario-introduced rows these columns stay NULL/`{}`; the canonical geometry and attributes for those rows live in `element_state` keyed by the introducing scenario. The table is **append-only** — nothing is ever deleted from it, not even when a scenario is removed, because Element IDs may be referenced by descendant scenarios, the audit log, or future restore features. A view called `element_baseline` filters to baseline rows only; everything that needs the "real territory" (exports, dashboards, future ingests) goes through that view. Only scenario-aware code reads the raw `element` table.

*Two example rows.*

| column | baseline row | scenario-introduced row |
|---|---|---|
| `id` | `b42` | `bN3` |
| `type_slug` | `"building"` | `"building"` |
| `parent_element_id` | `blk-1` | `blk-1` |
| `geometry` | real polygon | `NULL` |
| `attributes` | `{footprint_area: 200, num_floors: 2}` | `{}` |
| `origin` | `"baseline"` | `"scenario:S1"` |
| `source_record_ref` | `"inegi:CCV-2024-7a3f"` | `NULL` |

The baseline row is a real building ingested from INEGI, sitting on block `blk-1`. The scenario row is a hypothetical building proposed inside scenario S1 — its geometry and attributes are NULL here because the canonical values live in `element_state` keyed by `(S1, bN3)`.

#### Write layer

**`scenario`** — A scenario is a branch of the territorial state. It has a name, a lifecycle status (`draft` → `generating` → `ready` → `failed` → `archived`), and optionally a parent scenario it forked from. Comparison happens between scenarios; a user might compare a current draft to the baseline, or two siblings to each other. A `dirty` flag signals that interventions have been authored since the last successful generation — the UI's "Generate" button enables when `dirty = true`. When a user forks scenario A into B, the platform copies A's `element_state` rows into B at fork time so B starts from a known good state; B's worker then only needs to apply B's own interventions on top.

*Example row.*

| column | value |
|---|---|
| `id` | `S1` |
| `project_id` | `P1` |
| `parent_scenario_id` | `NULL` (forked directly from baseline) |
| `name` | `"Densify Av. Amsterdam — +200 buildings"` |
| `status` | `"ready"` |
| `dirty` | `false` |
| `current_job_id` | `NULL` (no generation running) |
| `created_by` | Ana López |
| `created_at` | `2026-05-08` |

Reads as: *"Ana's what-if on densifying Av. Amsterdam. It's been generated successfully and she hasn't edited anything since — the Generate button is greyed out, the compare panel is up to date."* Exactly one row per what-if; the actual edits, geometries, and indicator numbers live in `intervention`, `element_state`, and `element_aoi_state`, all keyed by `S1`.

**`intervention`** — One row per user edit inside a scenario: adding a new Element, modifying attributes/geometry of an existing one, or deleting one. This is the **append-only event log** of the scenario — no row is ever updated in place. The `sequence_num` column gives a deterministic replay order so the worker always produces the same `element_state` from the same intervention log. The `attribute_delta` column is JSONB carrying the change payload: geometry lives under a reserved `geometry` key, and scalar attribute changes (number of floors, land use, etc.) sit alongside it. For the `add` operation, `attribute_delta` holds the full initial state plus two extra columns (`element_type`, `parent_element_id`) that record the new Element's identity. The intervention table is never queried spatially; it's an event log read in sequence by the worker.

*Two example rows in scenario S1, in order of authoring.*

| column | modify | add |
|---|---|---|
| `scenario_id` | `S1` | `S1` |
| `element_id` | `b42` | `bN3` |
| `operation` | `"modify"` | `"add"` |
| `attribute_delta` | `{"num_floors": 5}` | `{"geometry": {...}, "footprint_area": 100, "num_floors": 4}` |
| `sequence_num` | `1` | `2` |

The first row says *"in S1, change building b42 to have 5 floors"* — the other attributes (footprint_area, …) and the geometry are inherited from the parent state. The second says *"in S1, add a new building bN3 with this geometry and these attributes."* The worker replays them in `sequence_num` order during Stage 2.

#### Projection layer

**`element_state`** — The worker's output and the read model the compare panel queries. **One row per (scenario_id, element_id)**, holding the resolved geometry, attributes, and computed indicator values for that Element in that scenario. Built fresh by the worker every Generate: it starts from the parent scenario's `element_state` (or from baseline if the scenario is a root), applies this scenario's interventions in sequence order, then runs per-Element indicators on top and writes the results back into the same row's `indicators` JSONB column. This is the table that makes reads fast at any intervention count — querying it is a single primary-key lookup or GiST-indexed spatial query, independent of how many interventions the scenario contains. The API treats it as read-only; only the worker writes.

*Example row — building b42 inside scenario S1, after Generate.*

| column | value |
|---|---|
| `scenario_id` | `S1` |
| `element_id` | `b42` |
| `geometry` | same polygon as baseline (no geometry intervention) |
| `attributes` | `{footprint_area: 200, num_floors: 5}` (modify bumped floors 2 → 5) |
| `indicators` | `{"building_density": 0.50}` (computed by Stage 4) |
| `generation_id` | `G7` |
| `computed_at` | `2026-05-09 09:46:58` |

Reads as: *"As of generation G7, in scenario S1, building b42 has 5 floors and contributes to a block density of 0.50."*

**`element_aoi_state`** — The scenario-scoped AoI overlap table. **One row per (scenario_id, element_id, aoi_id)** saying how much of that Element falls inside that AoI in that scenario. The need for this table is the most subtle architectural point in the design: scenario edits change geometry, and the baseline AoI overlap precomputed against unchanged geometry doesn't reflect those edits. An *added* building has no baseline overlap row, so it would silently disappear from AoI aggregates. A *moved* building keeps its baseline overlap row associated with the old AoI, so its contribution gets counted in the wrong place. Without this table, the compare panel would show numbers that physically contradict what the user sees on the map. The worker rebuilds this table for each scenario at generation time.

*Example row.*

| column | value |
|---|---|
| `scenario_id` | `S1` |
| `element_id` | `bN3` (the new building added by intervention) |
| `aoi_id` | `AOI-A` (Av. Amsterdam corridor) |
| `overlap_area` | `100.0` m² |
| `overlap_ratio` | `1.0` |

Reads as: *"In scenario S1, the new building bN3 lies entirely inside the Av. Amsterdam corridor AoI — its full 100 m² counts toward that AoI's aggregates."*

**`element_aoi_baseline`** — Same shape as `element_aoi_state` but for baseline geometry only, written once during baseline ingest. This is purely a performance optimisation: during a scenario Generate, the worker uses it as a fast-path source. For every Element whose geometry in the scenario matches its baseline geometry (i.e. no intervention touched it), the worker copies the matching `element_aoi_baseline` row into `element_aoi_state` instead of recomputing `ST_Intersection` from scratch. Since most scenarios only touch a small fraction of Elements, the slow spatial-intersection work runs only on the changed Elements; the rest are O(1) row copies.

*Example row.*

| column | value |
|---|---|
| `element_id` | `b42` (the baseline building) |
| `aoi_id` | `AOI-A` (Av. Amsterdam corridor) |
| `overlap_area` | `200.0` m² |
| `overlap_ratio` | `1.0` |

Reads as: *"In baseline geometry, building b42 lies entirely inside AOI-A."* When the worker generates scenario S1, b42's geometry was not touched by any intervention — so this row is copied as-is into `element_aoi_state(S1, b42, AOI-A)`, skipping the expensive `ST_Intersection`.

**`scenario_generation`** — Audit log of generation runs. One row per Generate attempt, recording: which scenario was generated, which `job_run` did the work, which version of the indicator code ran (`indicator_code_version` — the worker's git SHA, baked into the container at build time), how many interventions were processed, the start/completion times, and the outcome (succeeded / failed with error message). This is the audit trail that makes comparison across scenarios trustworthy: if two scenarios were generated against different indicator code versions, the compare panel can warn the user. It's also what makes partial recompute possible to add later without invalidating historical generations — the metadata captures exactly what produced any given `element_state` snapshot.

*Example row — the Generate that produced scenario S1's current state.*

| column | value |
|---|---|
| `id` | `G7` |
| `scenario_id` | `S1` |
| `job_run_id` | `J42` |
| `parent_generation_id` | `NULL` (root scenario, no parent generation) |
| `intervention_count` | `2` (the modify + add shown earlier) |
| `indicator_code_version` | `"git:a1b2c3d"` |
| `baseline_dataset_version` | `"inegi-2024-q3"` |
| `started_at` | `2026-05-09 09:46:30` |
| `completed_at` | `2026-05-09 09:47:02` |
| `status` | `"succeeded"` |

Reads as: *"Generation G7, which ran scenario S1 to completion against indicator-code commit a1b2c3d on the INEGI Q3 2024 baseline, took 32 seconds and processed 2 interventions."* Every `element_state` row produced by this run carries `generation_id = G7` so its provenance is recoverable forever.

#### Infrastructure

**`job_run`** — The generic worker-job table, not specific to scenarios. It's the queue + state machine for any background work in the platform; scenario generation is one job kind among potentially many (raster ingest, data refresh, indicator catalog seeding, etc.). One row per job, with queue state, claim ownership, payload, status, result/error. When the user clicks Generate, the API inserts a `job_run` row; the worker polls the table, claims the row, runs the generation, and updates the status. The frontend polls `GET /jobs/{id}` to show progress while the scenario is generating.

*Example row — the job that did generation G7.*

| column | value |
|---|---|
| `id` | `J42` |
| `kind` | `"scenario_generation"` |
| `status` | `"succeeded"` |
| `payload` | `{"scenario_id": "S1", "generation_id": "G7"}` |
| `claimed_by` | `"worker-pod-3"` |
| `claimed_at` | `2026-05-09 09:46:28` |
| `started_at` | `2026-05-09 09:46:30` |
| `finished_at` | `2026-05-09 09:47:02` |
| `error` | `NULL` |

Reads as: *"Job J42 was a scenario-generation job that ran on worker-pod-3 and finished successfully."* The same table also holds rows for raster-ingest jobs, indicator-seed jobs, etc., distinguished by the `kind` column.

---

## How the system works

The platform runs in five stages. Stages 1 and 5 are user-facing (the API serves them in milliseconds). Stages 2, 3 and 4 run inside the worker during a Generate.

| Stage | What happens | Who does it |
|---|---|---|
| 1 | User edits the map; one intervention is recorded; scenario marked "dirty" | API |
| 2 | All interventions for the scenario are replayed; resolved Element state is written | Worker |
| 3 | Scenario-scoped AoI overlap is rebuilt | Worker |
| 4 | Indicators are computed per Element on the resolved state | Worker |
| 5 | Compare panel reads two scenarios' indicator aggregates side by side | API |

### Stage 1 — Author interventions

**What happens.** The user sketches an edit on the map (move a lot, change a building's height, add a new wall). The frontend turns it into a JSON payload and POSTs it. The API writes one intervention row, sets the scenario's `dirty` flag, and returns. No background work runs yet — the indicator panel shows "stale, click Generate."

The payload for adding a new Element looks like this:

```
POST /scenarios/S/interventions
{ "operation":         "add",
  "element_type":      "building",
  "parent_element_id": "<block-1 uuid>",
  "attribute_delta": {
    "geometry":       { "type": "Polygon", "coordinates": [...] },
    "footprint_area": 100,
    "num_floors":     4
  }
}
```

For `add`, the payload carries `element_type` and `parent_element_id` explicitly — these are structural facts about the new Element that don't exist anywhere else yet. For `modify` and `delete`, both come from the existing `element` row referenced by `element_id`; they're not part of the payload.

The API does three writes in one transaction:

```sql
BEGIN;
INSERT INTO element (id, type_slug, parent_element_id, origin)
  VALUES (gen_random_uuid(), :element_type, :parent_element_id, 'scenario:S')
  RETURNING id INTO :new_id;
INSERT INTO intervention (scenario_id, element_id, operation,
                          attribute_delta, sequence_num, created_by)
  VALUES ('S', :new_id, 'add', :delta,
          (SELECT COALESCE(MAX(sequence_num), 0) + 1
             FROM intervention WHERE scenario_id = 'S'),
          :user_id);
UPDATE scenario SET dirty = true WHERE id = 'S';
COMMIT;
```

### Stage 2 — Resolve the scenario state

**What happens.** When the user clicks Generate, the worker reads all interventions for the scenario (and inherits state from its parent scenario if it has one), replays them in order, and writes one row per Element to `element_state`. After this stage, the projection holds a complete picture of "every Element as it appears in this scenario."

The worker also cleans up geometry quirks (`ST_MakeValid`) — the frontend's drawing tools can produce self-intersecting polygons during fast drag, and we don't want them breaking downstream spatial operations.

```python
def generate(scenario_id, generation_id):
    parent_id = get_parent(scenario_id)  # NULL for root scenarios

    # Inherit parent's resolved state, stamping this generation's id
    # on every inherited row so provenance is recoverable.
    sql(
        """INSERT INTO element_state
             (scenario_id, element_id, geometry, attributes,
              indicators, generation_id, computed_at)
           SELECT :sid, element_id, geometry, attributes,
                  indicators, :gen_id, now()
           FROM element_state WHERE scenario_id = :parent""",
        dict(sid=scenario_id, parent=parent_id, gen_id=generation_id),
    )

    # Replay this scenario's interventions in sequence_num order.
    for iv in interventions_for(scenario_id):
        if iv.operation == "add":
            sql(
                """INSERT INTO element_state
                     (scenario_id, element_id, geometry, attributes,
                      indicators, generation_id)
                   VALUES (%(s)s, %(e)s,
                     ST_MakeValid(ST_GeomFromGeoJSON(%(d)s->'geometry')),
                     %(d)s::jsonb - 'geometry', '{}'::jsonb, %(g)s)""",
                dict(
                    s=scenario_id,
                    e=iv.element_id,
                    d=iv.attribute_delta,
                    g=generation_id,
                ),
            )
        elif iv.operation == "modify":
            sql(
                """UPDATE element_state SET
                     geometry = COALESCE(
                       ST_MakeValid(ST_GeomFromGeoJSON(%(d)s->'geometry')),
                       geometry),
                     attributes = attributes || (%(d)s::jsonb - 'geometry'),
                     generation_id = %(g)s,
                     computed_at = now()
                   WHERE scenario_id=%(s)s AND element_id=%(e)s""",
                dict(
                    d=iv.attribute_delta,
                    s=scenario_id,
                    e=iv.element_id,
                    g=generation_id,
                ),
            )
        elif iv.operation == "delete":
            sql(
                """DELETE FROM element_state
                   WHERE scenario_id=%s AND element_id=%s""",
                (scenario_id, iv.element_id),
            )
```

### Stage 3 — Recompute AoI overlap for this scenario

**What happens.** Indicator aggregates depend on which Elements overlap which AoIs. A change to a building's geometry or the addition of a new one changes those overlaps. The worker recomputes the overlap for every Element in the scenario, into `element_aoi_state`. For Elements whose geometry didn't change, it takes a fast path: copy the row from `element_aoi_baseline` (the precomputed baseline overlap) rather than recomputing. Typical scenarios touch a small fraction of Elements; the slow `ST_Intersection` only runs for those.

**Why this stage exists at all** — the brief silently assumes a single AoI overlap surface works for both baseline and scenarios. It can't. An added Element has no baseline overlap row at all; a moved Element has a stale one. Reusing baseline overlap for scenarios silently miscounts in the AoI aggregates, and the compare panel shows wrong numbers next to the user's edits. The scenario- scoped projection is what makes Stage 5 honest.

```python
# Fast path: for every Element in this scenario whose geometry was
# never touched by an intervention, copy the precomputed baseline
# overlap. Identifying "untouched" Elements: element_id is NOT in the
# scenario's intervention log (excluding the parent's interventions —
# those are already baked into the inherited element_state).
sql(
    """
    INSERT INTO element_aoi_state
        (scenario_id, element_id, aoi_id, overlap_area, overlap_ratio)
    SELECT %(s)s, b.element_id, b.aoi_id, b.overlap_area, b.overlap_ratio
    FROM element_aoi_baseline b
    JOIN element_state es
      ON es.scenario_id = %(s)s AND es.element_id = b.element_id
    WHERE b.element_id NOT IN (
      SELECT element_id FROM intervention WHERE scenario_id = %(s)s
    )
""",
    dict(s=scenario_id),
)

# Slow path: recompute for the rest — added Elements (no baseline row
# exists at all) plus modified Elements (the baseline row is stale).
sql(
    """
    INSERT INTO element_aoi_state
        (scenario_id, element_id, aoi_id, overlap_area, overlap_ratio)
    SELECT %(s)s, es.element_id, a.id,
           ST_Area(ST_Intersection(es.geometry, a.geometry)::geography),
           ST_Area(ST_Intersection(es.geometry, a.geometry)::geography)
             / NULLIF(ST_Area(es.geometry::geography), 0)
    FROM element_state es
    JOIN area_of_interest a
      ON es.geometry && a.geometry          -- bbox prefilter
     AND ST_Intersects(es.geometry, a.geometry)
    WHERE es.scenario_id = %(s)s
      AND es.element_id IN (
        SELECT element_id FROM intervention WHERE scenario_id = %(s)s
      )
""",
    dict(s=scenario_id),
)
```

### Stage 4 — Compute per-Element indicators

**What happens.** One pipeline runs per indicator slug (building density, COS, CUS, accessibility, etc.). Each reads from `element_state` filtered by `scenario_id`, computes a value per Element, and writes the result back into that Element's row. The shape of the queries is identical to the baseline indicator pipelines — only a `WHERE scenario_id = :S` clause is added. No indicator code needs to know about interventions; the resolve step in Stage 2 already absorbed all the scenario mechanics.

```python
def compute_building_density(scenario_id, conn):
    # ::geography cast critical: ST_Area on a 4326 geometry returns
    # degrees², not m². Silent indicator bug if forgotten.
    conn.execute(
        """
        UPDATE element_state es_block SET
          indicators = es_block.indicators
            || jsonb_build_object('building_density',
                COALESCE((
                  SELECT SUM((es_b.attributes->>'footprint_area')::numeric)
                  FROM element_state es_b
                  JOIN element e_b ON e_b.id = es_b.element_id
                  WHERE es_b.scenario_id = %(s)s
                    AND e_b.type_slug = 'building'
                    AND es_b.geometry && es_block.geometry      -- bbox prefilter
                    AND ST_Intersects(es_b.geometry, es_block.geometry)
                ), 0) / ST_Area(es_block.geometry::geography)::numeric)
        FROM element e
        WHERE es_block.element_id = e.id
          AND es_block.scenario_id = %(s)s
          AND e.type_slug = 'block'
    """,
        dict(s=scenario_id),
    )
```

### Stage 5 — Aggregate for the compare panel

**What happens.** The compare panel asks for indicators for two scenarios over the same AoI. The API runs the same aggregate query against `element_aoi_state` for each scenario — min, max, average across all Elements that overlap the AoI. Two reads, same shape, same projection table, same indicator code version (recorded by Stage 2's `scenario_generation` audit row).

The two numbers are comparable by construction: the query shapes match exactly, only `scenario_id` differs.

```
GET /scenarios/Baseline/indicators?aoi_id=A&keys=building_density
GET /scenarios/S/indicators?aoi_id=A&keys=building_density
```

```sql
SELECT
  MIN((es.indicators->>'building_density')::numeric) AS min,
  MAX((es.indicators->>'building_density')::numeric) AS max,
  AVG((es.indicators->>'building_density')::numeric) AS avg
FROM element_state es
JOIN element_aoi_state ao
  ON ao.scenario_id = es.scenario_id AND ao.element_id = es.element_id
WHERE es.scenario_id = :scenario
  AND ao.aoi_id      = :aoi
  AND es.indicators ? 'building_density';
```

### Worked example

Baseline `element_state`:

| element_id | type | attributes |
|---|---|---|
| block-1 | block | `{}` |
| building-1 | building | `{footprint_area: 200, num_floors: 2}` |
| building-2 | building | `{footprint_area: 150, num_floors: 3}` |

User forks **S1**, authors two interventions:

1. Modify building-1: `{"num_floors": 5}` (type already known from `element`)
2. Add building-3 (`element_type='building'`, `parent_element_id=block-1`), `attribute_delta = {"geometry": {...}, "footprint_area": 100, "num_floors": 4}`

Generate runs:

- **Stage 2** writes 4 rows in `element_state` for S1.
- **Stage 3** copies 3 baseline overlap rows (unchanged Elements), computes 1 fresh row for building-3.
- **Stage 4** computes `building_density` for block-1: `(200 + 150 + 100) / area(block-1) = 0.5`.
- Worker sets `scenario.dirty=false`, `status='ready'`.

Compare panel for AoA = A renders **Baseline 0.4 → S1 0.5** (+0.1).

### Edge cases the design handles

- **Edit during generate.** Edit sets `dirty=true`. The in-flight job finishes against its frozen view; UI sees `dirty=true` and prompts another Generate.
- **Generate fails.** The worker runs the resolve, AoI-overlap, and indicator stages inside one transaction that rewrites `element_state` and `element_aoi_state` for the scenario; on failure it rolls back, leaving the previous successful generation's rows in place. `scenario_generation.status='failed'` and the error message persist for the UI. User retries.
- **Edit a scenario whose child exists.** S's `element_state` becomes stale; S' is unaffected until *its* user clicks Generate. The child's stale-parent risk is bounded by an explicit user action.
- **Concurrent generates of one scenario.** Forbidden by a partial unique index on `scenario(id) WHERE status='generating'`. Second request returns 202 pointing at the in-flight job.
- **Indicator code bumps mid-flight.** `scenario_generation` records `indicator_code_version` per run. Compare panels can warn or invalidate accordingly.

### Performance characteristics

- **Stage 1 (write intervention)**: O(1).
- **Stage 2 (resolve)**: O(parent rows + interventions). Sub-second at Phase 2 scale (a few thousand Elements per AoI, tens of interventions per scenario).
- **Stage 3 (AOI overlap)**: O(changed Elements × AoIs). Fast-path keeps the cost at "tens of `ST_Intersection` calls per scenario."
- **Stage 4 (indicators)**: same complexity as the baseline indicator pipeline.
- **Stage 5 (compare panel read)**: two paginated reads against the GiST-indexed projection. Independent of intervention count.

### Where this design comes from

None of the six patterns it relies on are invented for this project. A senior engineer joining the team should recognise every one of them within a few minutes.

| Pattern | Industry name | Where it ships |
|---|---|---|
| Append-only intervention log | Event sourcing | EventStore, Kafka, Datomic |
| Worker writes projection, API reads only | CQRS read model | Modern transactional systems |
| Scenario as branch + projection | Branch-and-project | Git, Dolt, Figma |
| Per-scenario AOI overlap | Per-branch materialised view | Multi-tenant analytics |
| `dirty` flag + explicit Generate | Lambda-architecture serving | Data products mixing edits + batch compute |
| Append-only catalog with `origin` | Type-1 SCD with provenance | Data warehouses |

Recovery from corruption is straightforward: rebuild `element_state` from the intervention log. Failure modes for each pattern are well documented across the industry.

---

## Schema details

This section drops to schema-level. Up to now the document has described what each table is *for*; the rest describes exactly what each table *is*, with SQL. Read this if you're implementing the schemas; skip it if you only need the conceptual picture.

### Supporting tables (referenced by FKs below)

These tables exist to make the rest of the model FK-resolvable. Only the columns load-bearing for Phase 2 are shown.

```sql
TABLE project (
  id                       uuid PRIMARY KEY,
  name                     text NOT NULL,
  -- Current default dataset version for this project. Scenarios pin
  -- their actual run version via scenario_generation; this column is
  -- what new scenarios start from.
  baseline_dataset_version text NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now()
);

TABLE area_of_interest (
  id         uuid PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES project(id),
  name       text NOT NULL,
  geometry   geometry(MultiPolygon, 4326) NOT NULL
    CHECK (ST_IsValid(geometry)),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX aoi_geom_gist ON area_of_interest USING GIST (geometry);

TABLE job_run (
  id          uuid PRIMARY KEY,
  -- Discriminator: 'scenario_generation', 'raster_ingest', etc.
  kind        text NOT NULL,
  status      text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  payload     jsonb NOT NULL DEFAULT '{}',
  claimed_by  text,
  claimed_at  timestamptz,
  started_at  timestamptz,
  finished_at timestamptz,
  error       text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_run_queue ON job_run (kind, status, created_at);
```

### The Element catalog

A single `element` table holds both baseline (real-world) Elements and Elements introduced by scenarios. An `origin` column distinguishes them.

```sql
TABLE element (
  id                uuid PRIMARY KEY,
  type_slug         text NOT NULL,
  parent_element_id uuid REFERENCES element(id),
  -- Canonical baseline geometry for origin='baseline' rows.
  -- NULL for scenario-introduced rows — their canonical geometry
  -- lives in element_state under the introducing scenario_id.
  -- Never read except by baseline ingest + the baseline element_state seed.
  geometry          geometry(Geometry, 4326),
  -- Same asymmetry: populated for baseline rows, '{}' for scenario rows.
  attributes        jsonb NOT NULL DEFAULT '{}',
  -- Provenance: where this Element was first introduced.
  origin            text NOT NULL DEFAULT 'baseline'
    CHECK (origin = 'baseline' OR origin LIKE 'scenario:%'),
  -- Upstream record identifier for ingest traceability (INEGI feature id,
  -- Overture GERS id, etc.). NULL for scenario-introduced rows.
  source_record_ref text,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX element_geom_gist  ON element USING GIST (geometry)
  WHERE geometry IS NOT NULL;
CREATE INDEX element_origin     ON element (origin);
CREATE INDEX element_parent     ON element (parent_element_id);
CREATE INDEX element_type_slug  ON element (type_slug);
```

The catalog is **append-only**: INSERT happens on baseline ingest and on `add` interventions, but no operation deletes from it (not even scenario deletion). The reason is that Element IDs can be referenced by descendant scenarios long after the scenario that introduced them is gone; treating identity as permanent removes a class of referential-integrity problems.

To keep canonical baseline data clean, every reader of "the real territorial catalog" goes through a view:

```sql
CREATE VIEW element_baseline AS
  SELECT * FROM element WHERE origin = 'baseline';
```

New baseline readers (exports, dashboards, future ingests) point at `element_baseline`, never the raw `element` table. A contract test enforces this convention so the boundary stays clean over years.

Scenario-aware code paths (indicator pipelines, `element_state` resolution) read `element` directly — they need to see scenario- introduced rows.

The alternative — two tables (`element` for baseline, `scenario_element` for hypothetical) — would give cleaner catalog purity at the cost of two ID spaces, denormalised type-slug in `element_state`, and UNION queries when the catalog is read whole. The single-table model with the view convention is simpler and the filter discipline is enforceable by test.

### Interventions

The brief's §9.1 proposes an entity-attribute-value child table for intervention payloads, with separate columns per scalar type. That pattern is obsolete in modern Postgres — a single JSONB column carries the same information, handles new value types without schema migrations, and lets the resolve step strip out the reserved `geometry` key cleanly.

```sql
TABLE intervention (
  id              uuid PRIMARY KEY,
  scenario_id     uuid NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
  element_id      uuid NOT NULL REFERENCES element(id),
  operation       text NOT NULL CHECK (operation IN ('add','modify','delete')),
  -- All deltas (scalars + geometry) as one JSONB blob.
  -- Reserved key 'geometry' carries GeoJSON for a move/reshape.
  -- Semantics depend on operation:
  --   add    → full initial state (geometry + all initial attributes)
  --   modify → only the keys that change
  --   delete → must be empty
  attribute_delta jsonb NOT NULL DEFAULT '{}'::jsonb,
  sequence_num    bigint NOT NULL,
  created_by      uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scenario_id, sequence_num),
  CHECK (operation <> 'delete' OR attribute_delta = '{}'::jsonb)
);
CREATE INDEX intervention_element ON intervention (element_id);
```

`sequence_num` is allocated inside the same transaction that writes the intervention: `SELECT COALESCE(MAX(sequence_num), 0) + 1 FROM intervention WHERE scenario_id = :s`. The UNIQUE constraint catches any race.

The intervention table is never queried spatially — it's an event log read sequentially during scenario generation. GiST is not needed on it. Invalid GeoJSON in `attribute_delta` is detected at resolve time rather than at INSERT; the failure path (generation marked failed) is recoverable by editing or deleting the bad intervention.

### The resolved state projection

`element_state` is the read model — a physical table the worker populates and the API reads. Its existence is what makes scenario indicator reads fast at any intervention count, and what makes compare panels comparable apples-to-apples.

```sql
TABLE element_state (
  scenario_id   uuid NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
  element_id    uuid NOT NULL REFERENCES element(id),
  geometry      geometry(Geometry, 4326) NOT NULL
    CHECK (ST_IsValid(geometry)),     -- last line of defence
  attributes    jsonb NOT NULL DEFAULT '{}',
  indicators    jsonb NOT NULL DEFAULT '{}',
  -- The generation that produced this row. Lets a reader recover
  -- provenance (indicator_code_version, dataset_version) without
  -- timestamp matching.
  generation_id uuid NOT NULL REFERENCES scenario_generation(id),
  computed_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scenario_id, element_id)
);
CREATE INDEX element_state_geom_gist ON element_state USING GIST (geometry);
```

The `ST_IsValid` CHECK is the last line of defence. The worker calls `ST_MakeValid()` on every parsed geometry before writing — interactive drawing tools can produce self-intersecting polygons on fast drag operations, and these would break downstream `ST_Intersection` / `ST_Area` calls.

**Spatial maths uses a metric CRS or geography cast.** Indicator queries that compute area or distance either cast `::geography` (sub-metre accuracy at planet scale) or reproject to a project-level metric CRS. Using `ST_Area()` directly on a 4326 geometry returns degrees², not m² — silently wrong indicators.

### Scenarios and their lifecycle

```sql
TABLE scenario (
  id                 uuid PRIMARY KEY,
  project_id         uuid NOT NULL REFERENCES project(id),
  parent_scenario_id uuid REFERENCES scenario(id),
  name               text NOT NULL,
  status             text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','generating','ready','failed','archived')),
  dirty              boolean NOT NULL DEFAULT false,
  -- Fast-path denormalisation: latest non-terminal scenario_generation.
  -- Source of truth is scenario_generation; this column is a UI cache.
  current_job_id     uuid REFERENCES job_run(id),
  created_by         uuid NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
CREATE INDEX scenario_project_status ON scenario (project_id, status);
CREATE INDEX scenario_parent         ON scenario (parent_scenario_id);
-- At most one in-flight generation per scenario, enforced by Postgres.
CREATE UNIQUE INDEX scenario_one_running_job
  ON scenario (id) WHERE status = 'generating';
```

The lifecycle rules:

1. **Hard delete is forbidden when descendants exist.** A scenario with children must be archived first, or the parent itself archived (reversible).
2. **Forks require parent `status='ready'`.** Forking a generating or failed scenario returns 409. Fork copies the parent's `element_state` rows in one transaction.
3. **Scenario delete cascades** to its own `intervention` and `element_state` rows. The `element` catalog is never touched — append-only is the rule.
4. **The `dirty` flag is the read-side signal.** UI's Generate button enables iff `dirty=true`. CRUD on interventions never auto- triggers a job; only `POST /scenarios/{id}/generate` does.
5. **Concurrent generates are blocked at the DB level** by the partial unique index above. The API's POST `/generate` does:
   ```sql
   UPDATE scenario SET status='generating', current_job_id=:job
   WHERE id=:s AND status<>'generating';
   ```
Zero rows updated → return 202 with the existing `current_job_id`.

Every successful generation records metadata for partial-recompute trustworthiness:

```sql
TABLE scenario_generation (
  id                       uuid PRIMARY KEY,
  scenario_id              uuid NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
  job_run_id               uuid NOT NULL REFERENCES job_run(id),
  -- The generation this run inherited from (parent scenario's last
  -- successful generation, or NULL if this scenario is a root).
  parent_generation_id     uuid REFERENCES scenario_generation(id),
  intervention_count       integer NOT NULL,
  indicator_code_version   text NOT NULL,
  -- The dataset version this run was computed against. Persisted here
  -- (rather than read from project at compare time) so historical
  -- generations remain meaningful after project.baseline_dataset_version
  -- rolls forward.
  baseline_dataset_version text NOT NULL,
  started_at               timestamptz NOT NULL,
  completed_at             timestamptz,
  status                   text NOT NULL CHECK (status IN ('running','succeeded','failed'))
);
CREATE INDEX scenario_generation_scenario ON scenario_generation (scenario_id, started_at DESC);
```

`indicator_code_version` is the worker's git SHA, baked into the container at build time and logged on boot. Phase 2 ships full recompute; the metadata is enough that, when partial recompute is added later, historical generations remain trustworthy or can be safely invalidated by code-version bump.

### Scenario-scoped AOI overlap

The brief silently assumes a single AOI overlap surface serves both baseline and scenarios. It can't — overlap precomputed against baseline geometry has no rows for scenario-introduced Elements, and gives stale results for Elements whose geometry changed via intervention. Reusing it for scenario indicator aggregates would silently miscount.

```sql
TABLE element_aoi_state (
  scenario_id   uuid NOT NULL REFERENCES scenario(id),
  element_id    uuid NOT NULL REFERENCES element(id),
  aoi_id        uuid NOT NULL REFERENCES area_of_interest(id),
  -- Areas computed via ::geography cast → m². Never store degrees².
  overlap_area  double precision NOT NULL,    -- m²
  overlap_ratio double precision NOT NULL,    -- 0–1, dimensionless
  PRIMARY KEY (scenario_id, element_id, aoi_id)
);
-- AoA aggregate reads scan by (scenario_id, aoi_id); the PK only
-- covers the element-first lookup path.
CREATE INDEX element_aoi_state_aoi
  ON element_aoi_state (scenario_id, aoi_id);

-- Precomputed baseline overlap. Same shape as element_aoi_state minus
-- scenario_id. Written once during baseline ingest; read by Stage 3 as
-- the fast-path source for Elements whose geometry was not touched by
-- any intervention in the scenario being generated.
TABLE element_aoi_baseline (
  element_id    uuid NOT NULL REFERENCES element(id),
  aoi_id        uuid NOT NULL REFERENCES area_of_interest(id),
  overlap_area  double precision NOT NULL,    -- m²
  overlap_ratio double precision NOT NULL,    -- 0–1, dimensionless
  PRIMARY KEY (element_id, aoi_id)
);
CREATE INDEX element_aoi_baseline_aoi
  ON element_aoi_baseline (aoi_id);
```

Kept as a separate table (rather than merged into `element_aoi_state` with a sentinel scenario_id) because baseline overlap is written once at ingest and never deleted on scenario teardown; the lifecycle and retention story differs from per-scenario rows.

The failure modes if AOI overlap is reused across baseline and scenarios:

- **Added Element silently disappears.** A new building exists in `element_state` but has no baseline overlap row → AoI count unchanged → compare panel says "no change."
- **Moved Element counted in wrong AoI.** Baseline overlap still associates the building with the old AoI; new AoI's aggregate is unchanged. Compare panel shows numbers that contradict the screen.
- **Compare panel becomes apples-to-oranges.** Both sides aggregate against baseline overlap, even though scenario geometry has changed. Categorical metric error.

This is the part of the design that the brief silently breaks the most badly. Its absence makes every AOI-aggregated indicator silently wrong for any scenario with a moved or added Element.

### Job runtime

§7.6 + §11 specify *"a notebook/script packaged in a Docker image and run as an Azure Container Apps Job."* The recommendation here is the opposite: a single integrated server-side worker process running alongside the API in the same deployment, claiming jobs from a `job_run` table.

Why:

- **One job model, one state machine.** Two job systems means twice the operational surface — enqueue, monitor, debug, version, test, roll back, all duplicated.
- **Cold-start latency.** Container-Apps-style serverless jobs typically incur 30s–2min on first invocation after idle. That pushes Generate UX from "submit and watch progress" to "submit and wait."
- **Notebooks aren't deployment artefacts.** Cell-order dependencies, hidden state, awkward CI, no clean unit-test path. Plain Python modules are reviewable and testable.
- **Scenario indicators reuse the baseline indicator code.** Same runtime keeps that uniformity.

If isolated compute is ever genuinely needed (GPU, untrusted code), serverless containers become a *deployment target* for the same Python module — not a parallel runtime to coordinate with.

---

## Other design choices

These are smaller decisions worth nailing down explicitly. None are load-bearing on their own but they remove ambiguity from the implementation.

### POI model

§5 says *"POIs are not spatial objects, they are derived classifications."* Treat them as a project-configurable JSONLogic filter applied to existing Elements; do not replace amenity Element types with criteria. Worker computes POI hits per scenario at generate time, into a sibling projection `element_poi_state( scenario_id, element_id, criterion_id)` — same shape as `element_aoi_state`.

### Macrolot definition

§7.1 lists Macrolot as in-scope but doesn't define it. The schema needs a concrete definition before any code lands — e.g. *"Macrolot sits between AGEB and block, project-defined, a block belongs to exactly one macrolot per project."* Mid-project hierarchy refactors are expensive.

### Temporal versioning

§3.1 says *"keep valid_from/valid_to in mind even if Phase 2 makes limited use."* Easy to misread as "add the columns now." Phase 2 ships with `dataset_version` only, pinned per project. The bitemporal-vs-unitemporal-vs-snapshot architectural call gets made when there's a real second time-series to validate against.

### List endpoint pagination

§9.2's `GET /elements/{element_type}` and `GET /interventions` return flat arrays. Add bbox + cursor pagination from day one. For map rendering, prefer vector tiles over GeoJSON list endpoints.

### Element catalog hierarchy

§3 says *"a parent element, constrained by a set of valid parent types per element type."* Phase 2 enforces the hierarchy at the application layer (intervention validation, ingest validation) keyed by `element.type_slug`. A dedicated `element_type` lookup table with `valid_parent_types text[]` plus a trigger-level cycle check is the cleaner long-term home and should land before the second pilot deployment; for Phase 2 it is over-engineering relative to the small, hard-coded type set.

### Cross-AoI compare semantics

§4.3 allows comparing scenarios with different AoAs but doesn't define semantics. The platform renders two independent panels with no automatic diff. Users wanting a diff over a common boundary must select the same AoA for both panels.

