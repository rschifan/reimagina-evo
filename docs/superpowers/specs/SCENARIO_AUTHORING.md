# Design: scenario authoring — attribute overrides, and before/after

A scenario is a named hypothetical: set policy attributes over an area, let
the indicators that read them recompute, and read the result against the
baseline it departs from.

**Status:** design, fourth framing, scope cut. Written before the code.

---

## Intent

The contract is the design of an urban centrality. The platform describes the
city as it is; a scenario describes one that does not exist yet.

**Why this is the first thing to build.** Five of twelve indicators write zero
rows on real Mexicali data — the three `edificability_block_*`,
`urban_form_block_planned_floors`, and `livability_block_score` which consumes
them. They read `num_levels`, `unit_size`, `people_per_household` and `floors`,
and nothing supplies those. They are policy choices, not observations: Overture
carries `num_floors` on 1,152 of Mexicali's 387,273 buildings (0.30%), so
deriving them would invent 99.7% of the answer. A scenario is where the choice
legitimately lives, and these five are exactly the indicators that evaluate it.

---

## Scope, and why it stops here

A scenario can change anything the catalog declares as an input. The catalog
declares four data inputs. **This design covers one of them**, and the reason
is architectural rather than a matter of effort.

| Input | Changed by | In scope |
|---|---|---|
| `mex.block` attributes | overriding `num_levels`, `unit_size`, `people_per_household`, `floors` | **yes** |
| `global.poi` | adding or removing amenities | no |
| `global.road_network` | adding or removing streets | no |
| `global.building` | adding or removing footprints | no |

⚠ **The asset graph partitions overrides by scenario and layers not at all.**
`_partition_mapping` (`registry.py:242-257`) returns `identity` only when the
producer declares a `scenario` dimension; `_feature_layer` declares none
(`registry.py:43, 78-86`), so every layer→indicator edge is stored as
`baseline`, and r041's propagation arm is gated on
`WHERE d.partition_mapping = 'identity'` (`r041:431-432`). A version published
for `global.poi@scenario=S` stops there and never reaches the four
accessibility indicators. The registry argues for this deliberately: *"There
is one shared set of blocks, not one per what-if."*

So "add a park and watch park accessibility improve" is not a table and an
overlay — it needs either a `scenario` partition dimension on the data layers,
which flips the mapping on every indicator edge, or per-indicator wiring of
scenario assets, which is the opposite of a generic mechanism. **That is an
architecture decision and belongs in its own design**, which this one does not
pre-empt.

Overrides need none of it, because they are already scenario-partitioned. That
is why attribute scenarios work today and feature scenarios never have.

## Goals

- A scenario is **named, listed, found again**.
- A proposal is **a rule over an area**, so it re-applies after a re-ingest.
- **Before and after are comparable** — per block, in summary, and on the map.
- The five currently-empty indicators produce real values under a scenario.
- **Every failure says why.** A blank map after drawing names its cause.

## Non-goals

- Feature add/remove, and therefore roads, amenities and buildings — above.
- Scenario comparison against **another scenario**; baseline only.
- Versioning, branching, export/import.
- Runtime tenant isolation — the RLS pattern is matched, not fixed.
- Changing what the indicators compute. This supplies their inputs.

## Anti-goals — failure even if it works

- **A scenario you cannot find again.**
- **Silent anything.** A write that reports success and persists nothing; an
  edit whose removal leaves its effect on the map; a rule that widens because
  an AOI was deleted; a recompute that moves no number. Earlier drafts
  contained all four.
- **Silent defaults** — a value nobody chose.
- **Touching the baseline.**

---

## Constraints

Each verified against the code. ⚠ One entry in an earlier draft's version of
this table was false — it claimed `aoi_feature` has no FK on `feature_id`,
which it does (`r004:39-40`). The claim came from reading truncated `psql`
output. Entries here were re-checked.

| | |
|---|---|
| **API writes need their own connection** | `apps/api/deps.py:131-136` rolls back on the **success** path. `jobs.py:629-631`: *"the pooled one rolls back on teardown, which would make this look like it worked and change nothing."* |
| **Supersession is scoped to rows written** | `repositories.py:2617-2652` returns early on empty input and marks stale only the features being written. A feature dropping out of a recompute keeps its old `current` row. |
| **`valid_max` raises, it does not clamp** | `r016:310-312`. Three of these bounds are reachable from the authoring surface; see "The three ceilings". |
| **The anti-join has a scope** | Required for reads that emit rows — five call sites plus `indicator_overlay_tile`. Not inside an `EXISTS`. |
| **`network_version` / `poi_version` are dirty markers** | Seeded by `seed_demo_attributes.py`, produced by nothing. Overriding one recomputes against identical inputs and returns identical numbers. They must not appear in the authoring surface. |
| **RLS is cosmetic here** | Nothing sets `app.current_project_id` outside tests; owners bypass RLS (`r011`, `ENGINEERING_NOTES.md:112-120`). |
| **Bulk writes only** | `apply_overrides` publishes once per attribute; per-edit is quadratic. |
| **`now()` is transaction time** | Two edits written in one transaction share a `created_at`, so it cannot order them. |
| **A geometry type check is not a validity check** | `r004:23-24` constrains `ST_GeometryType`; a self-intersecting bowtie is still `ST_Polygon`. Validity needs `ST_IsValid` / `ST_MakeValid`. |
| **Migration head is r047** | The new migration is r048. |

---

## Design

### `app_core.scenario`

```sql
CREATE TABLE app_core.scenario (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES app_core.project(id) ON DELETE CASCADE,
  aoi_id      uuid     REFERENCES curated_geo.area_of_interest(id) ON DELETE SET NULL,
  name        text NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
```

RLS: `tenant_isolation` on `project_id`, matching the other two tables.

**`project_id` is required at creation**, derived from `--aoi-id` when given
and otherwise passed explicitly. It cannot simply "derive from the AOI",
because `aoi_id` is nullable — an earlier draft said it could.

⚠ **The AOI must belong to that project, and only `create_scenario` says so.**
The `aoi_id` FK above admits any AOI — nothing in the schema ties it to
`project_id` — so a scenario in project A could otherwise be bounded by
project B's AOI, and materialisation (which scopes by `aoi_id` alone) would
resolve overrides over B's blocks.
`reimagina.scenarios.authoring.create_scenario` refuses the mismatched pair
with `ScenarioNotFound`, which is a 404 over HTTP and exit 2 on the CLI. That
is a complete fix only because it is the column's only writer: the two
`UPDATE`s on the table touch `updated_at` and `materialized_at`, no surface
offers a PUT or PATCH, and `ON DELETE SET NULL` can only empty the column.
A new insert path must repeat the check — the schema will not, and RLS is
inert (`01_architecture_map.md`, deferred risk T6).

### `curated_geo.scenario_edit`

```sql
CREATE TABLE curated_geo.scenario_edit (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  ordinal       integer NOT NULL,
  geometry      geometry(Geometry, 4326) NOT NULL
                CHECK (ST_GeometryType(geometry) IN ('ST_Polygon','ST_MultiPolygon')),
  type_id       uuid NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
  attribute_key text NOT NULL,
  value_numeric double precision NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scenario_id, ordinal)
);
CREATE INDEX scenario_edit_scenario_idx ON curated_geo.scenario_edit (scenario_id);
CREATE INDEX scenario_edit_geometry_gist ON curated_geo.scenario_edit USING gist (geometry);
```

- **`ordinal`, not `created_at`, decides "later edits win."** `now()` is
  transaction-start time, so two edits written together are indistinguishable
  by timestamp. A unit test asserting "later wins" would pass or fail on
  ordering luck.
- **`type_id`** rather than a `'block'` string: a feature type is
  `(context, type_slug)` everywhere, and the FK makes a typo impossible.
- **The CHECK is a type check only.** Validity is enforced on write:
  the API repairs with `ST_MakeValid` + `ST_CollectionExtract(…, 3)` and
  rejects what does not survive. An earlier draft claimed the CHECK covered
  self-intersection; it does not.
- **No `value_text`** — every authorable attribute is numeric and
  `effective_numeric` reads `value_numeric` only.

No `project_id` and no RLS on this table. RLS does not propagate across the
FK, so the rules are tenant-visible even though the scenario row is not. Given
that RLS here is cosmetic anyway, this is declared rather than fixed.

### Which blocks a drawn area catches

**`ST_Covers(edit.geometry, f.geometry_centroid)`**, the rule the platform
already uses. `count_features_within_polygon` (`repositories.py:1771-1800`)
answers this for the selection panel on the centroid, with `ST_Covers` rather
than `ST_Contains` so a centroid on a shared edge is not dropped, and
`selection-filter.ts` mirrors it client-side so the two cannot disagree.

⚠ The sidebar count and the edit's reach still differ in one way, and the UI
must not claim otherwise: `count_features_within_polygon` reads
`serving_geo.feature_aoi_v`, which is AOI-scoped, while materialisation is
scoped to the scenario's AOI — so they agree *when the active AOI is the
scenario's*. The integration test asserts that case, not equality in general.

### The three ceilings

`valid_max` is enforced by a trigger that **raises**, and three bounds are
reachable from these four attributes:

| Indicator | Bound | Formula | Checkable from the catalog alone? |
|---|---|---|---|
| `edificability_block_density` | 2,000 | `10000 × levels / unit_size` | **yes** — area cancels |
| `edificability_block_housing_units` | 200,000 | `area × levels / unit_size` | no — needs block area |
| `edificability_block_population` | 750,000 | `housing_units × pph` | no — area and pph |

An earlier draft treated the first as *the* ceiling. It is one of three, and
the other two depend on the largest block the rule catches — the catalog's own
note records a measured Tlalpan block at 138,695 housing units at six levels
and 60 m², so doubling levels there exceeds the bound.

So **validation happens at materialisation, against the matched features**,
not in the form against a constant: compute each bound over the selection's
maximum area, refuse before writing, and name the block, the bound and the
value. The form additionally pre-checks the density bound, which it can,
because catching the common case early is worth one duplicated rule.

### Materialisation

`materialize_scenario(conn, *, scenario_id)`:

1. **Refuse if the scenario has no AOI.** `aoi_id` is `ON DELETE SET NULL`, so
   a deleted AOI would otherwise silently widen a scoped rule to every block
   in the database.
2. Select features by `type_id` whose centroid the edit geometry covers,
   scoped to the AOI through `aoi_feature`. Resolve collisions by `ordinal`.
3. **Check the three ceilings** against the matched features; refuse as a
   whole if any is exceeded.
4. Compute which features **lose** an override, delete those rows, keep the ids.
5. Apply the rest in **one** `apply_overrides` call.
6. `publish_scenario_version(conn, scenario_id)` with **no `attributes`
   argument** — reconciles retirements, which a targeted publish does not.
7. ⚠ **Supersede the results the removed overrides produced.** Mark this
   scenario's `current` rows `'stale'` for the step-4 ids and the indicators
   reading the removed attributes.

Step 7 is not optional. Supersession in `bulk_write_numeric_results` is scoped
to the features being written and returns early on empty input, so a block
that loses its override is skipped by `edificability.py:104`, never written,
and keeps its old `current` row for the tile to paint. Without it, removing
every edit leaves the choropleth untouched.

`'stale'` rather than a new status: the CHECK allows only `current`, `stale`,
`failed`, and `stale` is already the supersession marker.

Materialisation is **idempotent** and **reports how many features each edit
matched**. Zero is returned to the caller and surfaced, not logged.

**Recompute always materialises first.** The API's recompute path runs
materialisation and then `evaluate-scenario` in one call, so "edited but not
applied" cannot exist at run time. This is why the asset graph needs no
knowledge of `scenario_edit` and r048 needs no `backing_kind` change —
the graph sees overrides, which it already partitions by scenario.
`scenario show` reports an edit count and a materialised-at timestamp, so the
state is visible between runs.

### Comparison

Baseline and scenario results already coexist in `indicator_result` — r043's
whole design — and `acceptance_run.sh:242-269` already computes a per-feature
delta with a LEFT JOIN.

**`GET /scenarios/{id}/comparison?indicator_slug=…`** returns per-feature
`{baseline, scenario, delta, state}` plus a summary: features changed, mean
delta, and whether that is an improvement, which the catalog declares as
`higher_is_better`.

`state` is three-valued, and the third value is why it exists:

| state | meaning |
|---|---|
| `changed` | both values exist and differ |
| `unchanged` | both exist and are equal |
| `new` | **no baseline value** — the scenario computed something where the city has nothing |

⚠ `new` is the common case for the five indicators this design exists for.
They have no baseline rows on real data, so `scenario − baseline` is NULL and
a two-state model renders them as absent. An earlier draft said untouched
blocks "show as unchanged, not absent" and missed that the interesting blocks
have no baseline at all.

**On the map**, r048 extends `serving_geo.indicator_overlay_tile` with an
optional `delta` query param. It emits `scenario − baseline` where both exist
and a `state` property otherwise. Extending the existing function rather than
adding one keeps `TILE_FUNCTIONS` and `infra/tiles/layers.json` unchanged —
but `serving_geo` **is** changed, which the maintenance checklist must say and
an earlier draft's did not.

⚠ The delta ramp is **diverging and centred on zero**, visually distinct from
the sequential value ramp. A difference drawn in the value ramp reads as a
value.

### Transactions

Every write opens its own connection and commits, following `jobs.py`. None
uses `Depends(get_db_conn)`. A contract test asserts no handler in
`routers/scenarios.py` depends on the pooled connection — the failure it
prevents is a 200 response with nothing written.

### Deleting a scenario

One transaction: delete overrides → `publish_scenario_version` with no
attributes → mark results `'stale'` → delete `asset_materialization` rows →
delete the scenario row, cascading its edits.

The fourth step is not cosmetic: `asset_component_v` (`r041:405-417`) filters
materialisation rows on nothing, so a deleted scenario's partition would stay
a live node reporting stale forever, and `assets stale` would accumulate one
entry per deleted scenario naming something that no longer exists.

### The attribute allowlist

`AUTHORABLE_ATTRIBUTES` lives **beside `overridable_attributes()` in
`assets/registry.py`**, derived from it minus `geometry` and the two version
markers. Not in `indicators/catalog.py`: `registry.py` defers its
`SHIPPED_INDICATORS` import to function scope in three places to keep that
dependency one-way.

The contract test asserts it is a subset of the derived set and that the
exclusions are exactly those three — so a new indicator reading a new policy
attribute appears automatically, and the list cannot rot the way `poi_version`
did for six migrations.

### CLI

```bash
scenario create --name "Centralidad norte" --aoi-id <uuid>
scenario list / show --scenario-id <uuid>
scenario set-attribute --scenario-id <uuid> --geojson area.geojson \
    --levels 6 --unit-size 60 --people-per-household 3.6
scenario recompute --scenario-id <uuid>     # materialise + evaluate
scenario compare --scenario-id <uuid> --slug edificability_block_density
```

`--levels` writes both `floors` and `num_levels`: the same quantity under two
names (`seed_demo_attributes.py:23-40`), and two fields would leave
`planned_floors` on the baseline while edificability moved, so one proposal
would make the map contradict itself.

A scenario sets **all three** policy inputs, because `edificability.py:101-107`
skips any feature missing any of them.

### API

`GET`/`POST /scenarios`, `GET`/`DELETE /scenarios/{id}`,
`POST`/`DELETE /scenarios/{id}/edits`, `POST /scenarios/{id}/recompute`,
`GET /scenarios/{id}/comparison`.

**`GET /indicators` grows `scenario_id`**: the five revived indicators have
rows only under a scenario, so the picker would never offer them. ⚠ Use
`scenario_id = X OR scenario_id IS NULL` inside the probe — it is an `EXISTS`,
emits no rows, so the anti-join is unnecessary and would buy a correlated
`NOT EXISTS` per catalog row for nothing. `SCENARIOS_LAYER.md`'s baseline-only
note must be amended.

### Frontend

- A scenario picker beside the indicator picker: `(baseline)` plus the AOI's
  scenarios.
- Draw an area with the existing tools; one form — levels, unit size, people
  per household — pre-checked against the density ceiling.
- "Recompute", then a **value / delta** toggle.
- `activateIndicator` (`choropleth.ts:163-181`) passes `scenario_id` and
  `delta` into the tile URL beside `aoi_id` at `:180`.
- `listIndicators` (`api.ts:104`) takes only `aoiId` and must carry
  `scenario_id` too.

---

## The demo this supports

Draw a district, allow six storeys at 60 m² with 3.6 people per household,
recompute. Five indicators that write nothing on real Mexicali data produce
values; the delta view shows them as `new` rather than as a difference,
because the baseline has none; `livability_block_score` computes for the first
time on real data, because its heaviest input finally has rows.

That is one shape of scenario, honestly bounded. "Add a park and watch
accessibility improve" is the other shape and waits on the partition question.

## What this does not give you

- **No survival of `DROP DATABASE`** — `mexicali_run.sh` drops the database on
  every run without `--keep`. The rule re-applies after a *re-ingest*, which
  is a smaller claim than an earlier draft made.
- **Values only where the proposal reaches.** Outside the drawn area a block
  has no override, so for the five indicators there is no scenario row and no
  baseline row to fall back to. Correct, but the UI must say so rather than
  let a painted patch in a blank AOI read as a rendering fault.
- **No feature changes**, and therefore nothing about accessibility.
- **No runtime tenant isolation**, **no author**.

## The risk I am not closing

**Materialisation cost at city scale is unmeasured.** A rule over 19,058
blocks writes up to one override row per block per attribute — four of them —
and `publish_scenario_version` digests every row for an attribute. The
analogous membership pass measured nine minutes before `ST_Subdivide` brought
it to forty seconds, so the shape and the remedy are known. Measure a
full-district edit before assuming `recompute` can be synchronous; if it
cannot, use the job layer rather than a second path.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | `ordinal` decides which edit wins; `--levels` writes both keys; all three ceilings are derived, not hardcoded |
| `tests/integration` | materialise is idempotent; a removed edit retires its version **and** supersedes its results; materialise refuses without an AOI; a ceiling violation refuses before writing and names the block; an invalid polygon is repaired by the write path; delete removes overrides, versions, results and materialisation rows; `ST_Covers` matches the sidebar count when the active AOI is the scenario's |
| `tests/contract` | `AUTHORABLE_ATTRIBUTES` is the derived set minus exactly three; no `scenarios.py` handler depends on `get_db_conn` |
| `tests/performance` | an explain-plan contract on the materialisation select, per `PERFORMANCE.md`, which owns budgets and trips on sequential scans |
| `tests/e2e` | draw, set three values, recompute, and see the five indicators appear in the picker and paint inside the drawn area |

⚠ **The proof test needs its own fixture.** "The five produce rows under a
scenario on the synthetic territory" passes *today*, because
`synthetic_territory.py:117-120` seeds all four attributes on every block. The
test must build attribute-free blocks with `_helpers.insert_feature`, assert
the five write nothing, apply a scenario, and assert they then write values.

⚠ **The comparison test must cover `new`.** A two-state baseline/scenario
assertion passes on indicators that have baselines and silently misses the
five that do not — which are the point.

## Maintenance checklist

1. Migration r048 — two tables, RLS policy, indexes, and the
   `indicator_overlay_tile` delta parameter
2. **`serving_geo` changes** — the tile function gains `delta`
3. `explore` gains a scenario view
4. CLI listing reads from `explore`
5. Tests per the table above
6. Docs: `AGENTS.md`; `SCENARIOS_LAYER.md` (the partial FK reversal, the
   baseline-only picker, "results stay readable"); `TERMINOLOGY.md` ("every
   `scenario_id` column is a plain uuid" is no longer true);
   `ENGINEERING_NOTES.md:108-110` ("a scenario→project association that does
   not exist" — it now does); **`docs/delivery/03_database_and_api_reference.md`**,
   which is generated, hashed in `SHA256SUMS` and guarded by
   `test_repository_structure.py:427-478` — regenerate it and the manifest
7. **`acceptance_run.sh:185-223`** mints a scenario from `uuid5` with no
   `app_core.scenario` row, which would make `explore.scenario_v` report zero
   scenarios right after the Schedule A run creates one. Move it onto the new
   CLI.

## Left to the builder

- Request and response shapes.
- Whether `recompute` is synchronous or goes through the job layer — decide
  from the measurement above.
- Sidebar layout, and the diverging palette for delta mode.
- Whether `explore.scenario_v` exposes edit counts, override counts, or both.

---

## What the reviews changed

Four framings. The first two designed block-attribute authoring without
comparison; the third tried to cover all four inputs; this one cuts back to
what the asset graph actually supports. One builder review and two adversarial
reviews, with every finding verified against the code before acceptance.

**The scope cut.** The third framing's central mechanism — publish a scenario
version on a data layer so its indicators replan — cannot work:
`_partition_mapping` reads the producer's dimensions, layers declare none, and
r041's propagation is gated on `identity`. The graph deliberately shares
layers across scenarios. Feature add/remove therefore needs an architecture
decision, not a table.

**Would have shipped silent failures.** Writes persisting nothing; a removed
edit leaving its result on the map; a deleted AOI widening a rule to the whole
database; a recompute that moves no number.

**Would have failed at runtime.** Levels above a ceiling raise a plpgsql
exception that aborts every block at once — and there are three ceilings, two
of them area-dependent and uncheckable from the catalog alone.

**Was factually wrong.** `aoi_feature` has no FK (it has one — read from
truncated output); `ST_CoveredBy` justified by a precedent that says the
opposite; "every indicator endpoint already takes `scenario_id`";
`renderIndicator` as the tile-URL site; six overlay readers (five, plus the
tile); a geometry type CHECK described as a validity check; `created_at`
offered as an edit ordering; an RLS test that cannot pass because the test
role owns the tables; a proof test that passed before the feature existed.
