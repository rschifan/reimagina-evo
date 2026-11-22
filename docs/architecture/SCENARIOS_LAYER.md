# Scenarios Layer

How the platform records a proposed change to the city, and how that record turns into recomputed indicator results.

**Related documents:** `DATA_LAYER.md` — the city data scenarios read from (never modified). `INDICATORS_LAYER.md` — the computed outputs scenarios produce. `JOB_LAYER.md` — the `Orchestrator` port that executes a scenario run.

---

## What this layer does

A scenario is a named "what if" — a proposed state of the city that has not happened yet. It overlays changes on the baseline city without ever modifying it.

The workflow is:

```
project  →  scenario  →  scenario_edit (drawn areas)  →  scenario_override rows  →  submit_run  →  indicator_result
```

- **Project** — `app_core.project`, the workspace an area of interest and its scenarios belong to (`r001_schemas_and_extensions.py:26-34`).
- **Scenario** — a named row in `app_core.scenario` (r048), unique by name within its project. It is the addressed resource: created, listed, shown, recomputed, compared and deleted by id. The `scenario_id` columns on `scenario_override`, `indicator_result`, `job_run` and `aoi_indicator_summary` are still plain uuids with no FK (`r016_indicator_layer.py:7-8`, `r030_scenario_override.py:5-6`), so what ties them to the scenario row is code, not schema — see *The scenario* below.
- **Edit** — one drawn area carrying one attribute value, in `curated_geo.scenario_edit` (r048). This is what a person authors.
- **Override** — one per-feature attribute edit, in `curated_geo.scenario_override` (r030). Materialising a scenario expands its edits into override rows, and those rows are what the indicators read.
- **Run** — an execution submitted through the `Orchestrator` port. Defined in `JOB_LAYER.md`.
- **Indicator result** — the output, in `curated_geo.indicator_result` with `scenario_id` set (see `INDICATORS_LAYER.md`).

**The baseline is never touched.** An override is a *read-time overlay resolved in SQL*, not an edit to `feature` or `observation` and not an in-memory merge inside a worker. The effective value of an attribute is `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins (`effective_numeric`, `scenarios/overrides.py:169`).

**The same rule applies on the way out** (r043). A scenario stores only what it
changed, so an indicator it did not recompute has no row under it. Reads
therefore resolve as an overlay too: *this scenario's row where it has one, the
baseline's otherwise.* Before that, a `floors` override dirtied one indicator, a
run materialised 1 of 14, and the other 13 were simply missing under that
scenario — holes on the map, and `compute_composite` intersecting its components
to nothing and writing zero rows.

Six read paths carry it, plus the tile: `list_current_for_indicator`,
`get_current`, `fetch_aoi_children`, `list_current_with_geometry`,
`feature_indicator_ranks`, `value_stats`, and
`serving_geo.indicator_overlay_tile`. The predicate is
`storage/_utils.scenario_overlay`.

**The difference, on the same tile function** (r049).
`serving_geo.indicator_overlay_tile` takes an optional `delta` parameter and
then emits `delta` and `state` per feature instead of `value_numeric` and
`quality_flag`. The classification is `scenarios/comparison.py`'s, not a second
opinion: the effective value is `COALESCE(scenario, baseline)` resolved before
anything is classified, a feature with no baseline is `new` and carries no
delta, and both sides require a numeric value — so a map and the comparison
endpoint cannot report different counts for the same scenario. `delta` with no
scenario is an empty tile rather than the baseline's own values, which would
read as "my scenario did nothing".

⚠ **An anti-join, not `scenario_id = X OR scenario_id IS NULL`.** The widened
form looks equivalent and silently double-counts: where a scenario *has*
recomputed a feature, both rows match, so an aggregate averages it twice and a
tile emits two features with the same id. A baseline row qualifies only when no
scenario row exists for that `(feature, indicator)`, so exactly one row survives
per pair — and the predicate drops into a `WHERE`, a `JOIN … ON` or an aggregate
unchanged.

⚠ **A scenario row shadows the baseline only when it carries a value.** Keying
on row existence alone contradicts the `COALESCE` rule above:
`effective_numeric` resolves `COALESCE(override, observation, base)`, and
COALESCE skips a NULL override rather than letting it win.
`write_result(value_numeric=None, quality_flag="missing")` is a supported call,
so valueless current rows exist by design — and without the value test one of
them suppresses the baseline *and* is itself dropped by the readers' own
`value_numeric IS NOT NULL` filters, so the feature disappears from `value_stats`
and from every composite that consumes it rather than falling back.

The trade-off is deliberate: **a scenario cannot express "recomputed, and the
answer is now unknown" and have that beat the baseline.** Doing so needs a
distinct signal the read layer honours — a `quality_flag` it inspects — not the
absence of a value, which is indistinguishable from "not computed".

⚠ **The value test belongs on both arms of the anti-join.** Guarding only the
probe stops a valueless scenario row from *suppressing* the baseline, but leaves
`scenario_id = X` matching that row unconditionally — so the pair qualifies
twice and the one-row-per-pair invariant above is broken, which is the
double-count the anti-join exists to avoid, reintroduced one case down. It is
harder to see than the widened form, because it only bites once a scenario has
recorded a valueless row, and because every reader that keys by feature (a dict
comprehension, `dict(row)`, `fetchone()`, the client drawing an MVT tile)
collapses the duplicate to whichever row arrived *last* — so it reads as an
intermittent wrong value rather than as a duplicate. That order follows the
query plan: an index scan on `ir_scenario_slug_idx` returns the scenario row
first and the baseline wins, while a seq scan (which the planner prefers once
the table has statistics) puts it last and the read reports no value. In
`fetch_aoi_children`, which puts the predicate in a `LEFT JOIN … ON`, the
duplicate is not cosmetic: the feature's `overlap_measure` weight is counted
twice and the AOI summary is quietly wrong.

So a scenario row qualifies when it carries a value **or** when no baseline row
exists for the pair — the second half keeping a `missing` row the baseline never
computed from falling out of both arms and vanishing. Exactly one row survives
per pair in all four configurations: a valued scenario row wins, a valueless one
yields to the baseline, a valueless one with no baseline is still returned, and
no scenario row leaves the baseline.

⚠ **The rule lives in two places, and both must change together.**
`storage._utils.scenario_overlay` carries it for the six Python read paths;
`serving_geo.indicator_overlay_tile` carries a separate copy in SQL (`_OVERLAY`
in r049), because a tile function cannot call into Python. Both had the
one-sided form and both were fixed (#93 and r049). r049's `downgrade` keeps
r043's one-sided predicate, `_OVERLAY_R043`, so downgrading restores the
function r043 shipped rather than one that never existed at that revision.

⚠ **Writes stay exact-match.** Supersession is not a fallback question:
`_mark_existing_current_stale` and the write path key on `scenario_id` exactly.

⚠ **The indicator picker is the one read that uses the widened form, because
it emits no rows.** `list_indicators(with_values_for_aoi=, scenario_id=)` was
baseline-only, which hid every indicator that exists only inside a scenario —
the whole edificability family — from the picker that a scenario was authored
to drive. It now counts the named scenario's rows *and* the baseline's, written
as `(ir.scenario_id = %s OR ir.scenario_id IS NULL)` inside its `EXISTS` probe
over `indicator_catalog`.

That is not an exception to the rule above; it is the rule's boundary. The
anti-join exists because a read that *returns* rows must not match both a
feature's scenario row and its baseline row. This predicate sits inside an
`EXISTS`: it emits nothing and answers "is there at least one", so one matching
row and two are the same answer and double-counting is unrepresentable. The
anti-join would add a correlated `NOT EXISTS` per catalog row for an identical
result — measured at 34× the planner's cost on the dev dataset for the same 12
slugs. Both halves of the `OR` are load-bearing: baseline-only is the bug
above, and scenario-only would drop every indicator the scenario did not
recompute, each of which still renders through to its baseline value by the
overlay rule.

`scenario_id` is a *filter* here and not the resource being addressed, so a
well-formed id naming no scenario is answered with the baseline set — which is
also what would render — rather than refused. A malformed one is rejected at
the API boundary as 422, typed `UUID`, so it never reaches the `uuid` cast.
Pinned in `tests/integration/test_indicator_picker_scenario.py`, whose
`test_an_indicator_is_offered_exactly_once_under_a_scenario` gives one feature
both rows — the state a join would duplicate.

⚠ The previous version of this document described this layer in terms of `app_core.intervention`, `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. None of those tables exist and none were ever created. The mechanism is `curated_geo.scenario_edit` expanded into `curated_geo.scenario_override`, with `app_core.scenario` naming the whole.

---

## Tables

### app_core.project

Top-level workspace. Groups areas of interest.

```sql
-- r001_schemas_and_extensions.py:27-33
CREATE TABLE IF NOT EXISTS app_core.project (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text        NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
```

It exists because `curated_geo.area_of_interest.project_id` references it (`r004_aoi_tables.py:21`), and since r048 `app_core.scenario.project_id` does too.

---

### app_core.scenario

A named what-if inside a project (r048). `project_id` is required and `aoi_id` is not: a scenario may be created before any AOI exists, so the project cannot always be derived from the AOI. `UNIQUE (project_id, name)` is what lets a scenario be addressed by name. `materialized_at` is null until the drawn edits have first been expanded into overrides. Row-level security is enabled on this table, keyed on `project_id`, but it is **inert today**: the policy admits every row while `app.current_project_id` is unset, nothing outside tests sets it, and the API connects as the table owner, so it provides no tenant isolation yet (a known, deferred risk).

### curated_geo.scenario_edit

One drawn area carrying one attribute value, ordered by `ordinal` ("later edits win"; not `created_at`, because `now()` is transaction-start time). The geometry CHECK is a type check only, so `add_edit` repairs the area on write (`_repair_geometry`: `ST_MakeValid`, then its polygonal part) and refuses one with nothing polygonal left as `InvalidGeometry`. `attribute_key` must be one of `authorable_attributes()` (`assets/registry.py`) — today `floors`, `lot_coverage`, `num_levels`, `people_per_household` and `unit_size`: every overridable attribute except `geometry`, `poi_version` and `network_version`. A rule writes one edit per number it **sets** — levels (as both `floors` and `num_levels`), unit size, people per household, lot coverage — and nothing for the numbers it leaves out, so every block keeps its own value for those. At least one must be set. Writing all of them, as rules once did, flattened the area: a rule meant to change household size also gave every block the same storeys, unit size and coverage, and so the same density (`coverage × levels × 10 000 / unit size`, block area cancelling). Deleting the scenario cascades the edits.

**A rule is the edits one proposal wrote over one area.** Nothing in the schema groups them: `update_rule` treats a set of edit ids as one rule when every id is an edit of the scenario and all share one stored geometry, and the workspace groups a scenario's edits into rules the same way, by identical `geometry_wkt` (`groupEditsByArea` in `scenario.ts`). `PATCH /scenarios/{id}/rules` sends the rule's **whole** set of numbers — one left out is no longer set by the rule — and optionally its area, applied **in place**: the edits keep their ids and ordinals, so the rule keeps its position in "later wins" (deleting and re-adding would renumber it after every other rule, and editing rule 1 would make it beat rule 2 where they overlap). Ids that are not one rule of the scenario are a 422. A number the rule did not set before is appended as an edit over the rule's area, at the next ordinal; one it no longer sets is deleted. The PATCH does not recompute; the caller does.

**Lot coverage.** `lot_coverage` is the share of the block that is built on (COS, `0 < c ≤ 1`). The edificability family computes `housing_units = area × lot_coverage × num_levels / unit_size`; a block without `lot_coverage` is computed at 1.0, the whole block — the collaborators' formula exactly. The fitted baseline carries each block's real coverage (Overture footprint ÷ block area), so a rule's coverage is compared with what is built today. The API writes coverage only when it is sent; the CLI writes 1.0 unless `--lot-coverage` says otherwise; the scenarios page sends what the planner types. The ceiling checks (precheck and materialisation) count it.

### The scenario id — what the schema does not tie together

`curated_geo.scenario_override`, `curated_geo.indicator_result`, `jobs_runtime.job_run` and `curated_geo.aoi_indicator_summary` still take a plain `uuid` with no foreign key: r016 and r030 chose that while the entity was deferred, and r048 added the entity without adding the FKs. `NULL` means baseline — the city as it is today.

Consequences worth stating:

- Nothing in the schema enforces that a `scenario_id` on those four tables refers to a scenario row. A typo'd uuid produces a valid, empty scenario, and the low-level `apply_override` path (below) can still write overrides for an id that has no row.
- ⚠ **A scenario's AOI is tied to its project by code, not by the schema.** r048 declares `aoi_id` as a plain reference to `curated_geo.area_of_interest(id)`, so the database accepts a scenario in project A bounded by project B's AOI. Materialisation scopes its selection by `aoi_id` alone, so such a scenario would write overrides over B's features. `create_scenario` is the column's only writer and refuses the pair (`_require_aoi_in_project`); a new insert path has to repeat that check.
- Because those tables have no FK, deleting a scenario does not cascade into them. `delete_scenario` deletes the traces by hand, in one transaction under the scenario's row lock: the scenario's overrides, every `asset_version` and `asset_materialization` row of its partition, every `indicator_result` and `aoi_indicator_summary` row with its `scenario_id` (any status), and the `job_run` rows carrying its `scenario_id` (r016's trigger makes a result's scenario equal its run's, so those runs computed this scenario and nothing else; `job_unit.job_run_id` is set to NULL). Only then does it delete the row, which cascades the edits. Recompute runs (`orchestration_run`) are run history and stay until the jobs page clears them.

---

### curated_geo.scenario_override

One attribute edit for one feature under one scenario. Keyed like `observation`'s one-current-per-`(feature, attribute)` shape.

```sql
-- r030_scenario_override.py:27-46
CREATE TABLE curated_geo.scenario_override (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid        NOT NULL,
  feature_id    uuid        NOT NULL
                            REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
  attribute_key text        NOT NULL,
  value_numeric double precision,
  value_text    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scenario_id, feature_id, attribute_key),
  CHECK (num_nonnulls(value_numeric, value_text) = 1)
);

CREATE INDEX scenario_override_lookup_idx
  ON curated_geo.scenario_override (scenario_id, attribute_key);
```

The `CHECK` makes an override exactly one typed value — never both, never neither. The lookup index serves the change-set read below, which filters on `scenario_id` and returns `attribute_key`.

There is no `parameters` / model-configuration table. A scenario expresses itself entirely as per-feature attribute edits; model constants live in code.

---

### jobs_runtime.job_run

Defined in `JOB_LAYER.md`. Note that it is **not** created at submit time by this layer — the runner opens it when the compute starts (`jobs/_lifecycle.py:54`).

---

## How a scenario edit becomes recomputed results

```
1. Record the edit
     apply_override(conn, scenario_id=…, feature_id=…,
                    attribute_key="floors", value_numeric=10.0)   overrides.py:26
     → INSERT … ON CONFLICT (scenario_id, feature_id, attribute_key) DO UPDATE
     → publish_scenario_version(conn, scenario_id, ["floors"])     overrides.py:85
       -- a new version of scenario.override.floors at this scenario's
       -- partition; this is what freshness sees

2. Submit a run
     orchestrator.submit_run(conn, changed_attributes=…, edited_features=…,
                             scenario_id=…)                        JOB_LAYER.md

3. Plan it                                                         plan.py:174-204
     -- An explicit change-set wins: compute_dirty_set over it.
     -- Nothing named and an asset graph present: the dirty set comes from
     --   freshness at this scenario's partition (the versions step 1 published).
     -- Nothing named and no asset graph: resolve_change_set reads the diff
     --   back from the scenario's override rows (overrides.py:232-252) — the
     --   rows are the diff, so a caller need not re-declare what changed.

4. Run the dirty set                                               JOB_LAYER.md
     -- compute_dirty_set: every indicator whose declared `reads` intersects
     -- the changed attributes, plus everything transitively depending on it.

5. Each runner reads effective values, not baseline values
     effective_numeric(conn, type_id=…, attribute_key="floors",
                       scenario_id=…, feature_ids=…)               overrides.py:169

6. Results are written to curated_geo.indicator_result with the run's scenario_id
```

Step 3 is why an override edits `attribute_key` rather than some scenario-specific vocabulary: the key *is* the `changed_attributes` token the impact engine keys the dirty-set off, so no translation layer is needed (`overrides.py:9-10`).

**Attribute scoping depends on the path, and so does feature scoping.** A scenario submit that names nothing goes through the asset graph, where there is one override asset per overridable attribute (`assets/registry.py`, `scenario_override_assets`). Editing `floors` publishes a version for `scenario.override.floors` alone, so only the indicators declaring an edge to `floors` — and whatever is downstream of them — are recomputed. Editing an attribute no indicator reads recomputes nothing. That path is whole-indicator in *features*: every unit it plans covers every feature the indicator applies to (`plan.py:55-82`).

An explicit change-set — `POST /jobs` with `changed_attributes` / `edited_features`, and `POST /scenarios/{id}/recompute`, which passes the materialisation's footprint — goes through `impact.compute_dirty_set` (`impact.py:97`). Its attribute half is a flat union: every indicator reading *any* named attribute is recomputed over *every* named feature. Its feature half is scoped: a seed indicator gets the named features and a dependent the union of its inputs' features, which the runner passes on as `feature_ids` (`runners.py:103`). With no asset graph the scenario-only submit takes this path too, with `resolve_change_set`'s union of the override rows. Widening or remapping a feature set per indicator (`impact.FeatureExpander`) is a hook with only the identity implementation. With a `scenario_id` the graph still has a say: an indicator already computed for that scenario and AOI at its current version is dropped, and a run over the whole footprint is recorded as covering the AOI (`runners._recording`), so recomputing an unchanged scenario is a noop.

---

## Effective attribute resolution

The overlay is one `COALESCE` over three sources, evaluated in the database (`overrides.py:191-211`):

```sql
SELECT f.id::text,
       COALESCE(
         so.value_numeric,                                          -- scenario override
         obs.value_numeric,                                         -- current observation
         NULLIF(f.attributes->>%(key)s, '')::double precision       -- base attribute
       )
FROM curated_geo.feature f
LEFT JOIN curated_geo.observation obs
       ON obs.feature_id = f.id AND obs.attribute_key = %(key)s AND obs.is_current
LEFT JOIN curated_geo.scenario_override so
       ON so.feature_id = f.id AND so.attribute_key = %(key)s
      AND so.scenario_id = %(scenario)s
WHERE f.type_id = %(type_id)s
  -- AND f.id = ANY(%(fids)s::uuid[])  when feature_ids is given
```

With `scenario_id = NULL` the override join matches nothing and the expression collapses to `observation ⊕ base` — the baseline. The same function therefore serves both modes, which is what makes a base/scenario comparison an apples-to-apples one.

Only features whose effective value is non-null are returned (`overrides.py:179`, `:212`). Postgres only — the function rejects any other backend (`overrides.py:21-23`).

`compute_planned_floors` is the worked example: it reads the effective `floors` per block through this function (`indicators/planned_floors.py:39-45`) and writes the result inside a `job_run` context (`:47-58`).

---

## How baseline data participates in scenarios

Scenarios do not replace the baseline city. They sit on top of it. Three kinds of information participate in a run:

| Data role | Where it lives | How scenarios use it |
|---|---|---|
| City-defining baseline | `curated_geo.feature.attributes` | The stable objects, geometries, and base attributes every indicator starts from |
| Baseline enrichments | `curated_geo.observation` | Read-only evidence — population, NDVI, sidewalk width, footfall — overriding the base attribute when current |
| Scenario edits | `curated_geo.scenario_override` | Overrides a selected `(feature, attribute)` for one scenario, ranking above both |

This produces one rule:

- baseline `feature` and `observation` rows are never edited by a scenario,
- only the resolved effective value differs during computation,
- outputs are written as new `indicator_result` rows carrying that `scenario_id`.

Examples:

- A scenario may override `floors` for a block without rewriting the block's baseline attributes.
- A scenario may still use census population or NDVI observations as read-only evidence while recomputing.

---

## The HTTP surface

`apps/api/routers/scenarios.py`, mounted at `/scenarios`. Every handler opens its own committed connection (`contextlib.closing(connect(api_dsn()))`), never the pooled `get_db_conn`, which rolls back on success. A malformed scenario id in the path answers 404, as an unknown one does; a malformed uuid in a query or body field answers 422. The API's CORS policy allows `PATCH` alongside `GET`, `POST`, `DELETE` and `OPTIONS` (`apps/api/app.py`), so `PATCH …/rules` also works from a cross-origin frontend.

| Method | Path | Body / query | Success | Refusals |
|---|---|---|---|---|
| GET | `/scenarios` | `project_id`, `aoi_id` (both optional) | 200: rows newest first, each with `edit_count` and `latest_run` (`{id, status}` or `null`) | — |
| POST | `/scenarios` | `project_id`, `name` (non-empty), `aoi_id`?, `description`? | 201: the scenario as `GET` returns it | 404 an AOI of another project, or (`unknown_reference`) a project or AOI that does not exist; 409 a duplicate name in the project |
| GET | `/scenarios/indicators` | — | 200: the indicators a rule can change, inputs first, each with `statistic` (`sum` for `units` and `people`, else `median`) | — |
| GET | `/scenarios/{scenario_id}` | — | 200: the scenario, `override_count`, and `edits` (`id`, `ordinal`, `type_id`, `attribute_key`, `value_numeric`, `geometry_wkt`, `created_at`) in ordinal order | 404 |
| DELETE | `/scenarios/{scenario_id}` | — | 200: `{scenario_id, overrides, versions_deleted, results_deleted, job_runs_deleted, materializations}` | 404, also on a second delete |
| POST | `/scenarios/{scenario_id}/edits` | `area` (GeoJSON FeatureCollection, Feature or geometry; polygons unioned), any of `levels`, `unit_size`, `people_per_household`, `lot_coverage`; `type_context` (`MEX`), `type_slug` (`block`) | 201: `{scenario_id, feature_type, edits: [{id, attribute_key, value_numeric}]}`, one edit per number set (`levels` → `floors` and `num_levels`) | 400 `empty_proposal`, `invalid_value` (non-positive or non-finite; coverage outside `0 < c ≤ 1`), `invalid_geometry`; 422 `ceiling_precheck_failed` with `levels`, `unit_size`, `allowed_levels`; 404 unknown scenario or feature type |
| PATCH | `/scenarios/{scenario_id}/rules` | `edit_ids` (≥ 1, one rule's edits), `area`? (omitted keeps it), the rule's whole set of numbers, `type_context`, `type_slug` | 200: the same shape, every edit of the rule after the update | as `POST …/edits`, plus 422 `not_one_rule` (`field: edit_ids`) |
| DELETE | `/scenarios/{scenario_id}/edits` | `edit_id` (query) | 204 | 404 unknown scenario, or an edit not in this scenario |
| POST | `/scenarios/{scenario_id}/recompute` | — | 202: `{scenario_id, matched_per_edit, overrides_written, overrides_removed, results_superseded, run_id, run_status, deduplicated}`; `run_id` is `null` on a noop | 404; 409 the scenario has no AOI; 422 `ceiling_exceeded` with `breach_count` and up to 20 `breaches`; 500 `recompute_not_enqueued` (materialised and committed, enqueue refused — retry is safe); 503 `ops_plane_not_provisioned` or `database_unavailable` |
| GET | `/scenarios/{scenario_id}/comparison` | `indicator_slug`, `aoi_id`?, `limit`? (≥ 1; omitted returns every row) | 200: `{scenario_id, indicator_slug, aoi_id, summary: {changed, unchanged, new, mean_delta, higher_is_better}, row_count, rows: [{feature_id, baseline, scenario, delta, state}], rows_truncated}` | 404 unknown scenario, indicator or AOI |

Neither `DELETE …/edits` nor `PATCH …/rules` recomputes: the overrides a removed or changed rule produced are withdrawn or rewritten by the next `recompute`. `recompute` is two transactions — the materialisation is committed before the run is enqueued — so the scenario's row lock is not held through the compute.

---

## Use cases

### Creating a new project

```sql
INSERT INTO app_core.project (name, description)
VALUES (
  'Centro Histórico Regeneration',
  'Urban renewal study for the downtown core area'
)
RETURNING id; -- :project_id
```

---

### Creating an area of interest for a project

The AOI is stored in the data layer but linked to a project.

```sql
INSERT INTO curated_geo.area_of_interest (project_id, name, description, geometry)
VALUES (
  :project_id,
  'Study boundary — downtown core',
  'Covers the 12 blocks within the historic district perimeter',
  ST_GeomFromGeoJSON(:aoi_geojson)
)
RETURNING id; -- :aoi_id
```

`python -m apps.cli.app aoi compute-features --aoi-id <uuid>` then populates `curated_geo.aoi_feature` — the AOI's membership, which every AOI-scoped read and the materialisation below scope by (`compute_aoi_features`, `data/aoi.py`).

---

### Authoring a scenario

Create the scenario, draw an area with the policy inputs, then recompute:

```bash
python -m apps.cli.app scenario create --name "Upzoning" --aoi-id <uuid>
python -m apps.cli.app scenario set-attribute --scenario-id <uuid> \
    --geojson area.geojson --levels 12 --unit-size 80 --people-per-household 3.2 \
    [--lot-coverage 0.6]
python -m apps.cli.app scenario recompute --scenario-id <uuid> [--sync | --detach]
python -m apps.cli.app scenario compare --scenario-id <uuid> \
    --slug urban_form_block_planned_floors
python -m apps.cli.app scenario list      # reads explore.scenario_v
python -m apps.cli.app scenario show --scenario-id <uuid>
```

`set-attribute` requires `--levels`, `--unit-size` and `--people-per-household` and always writes lot coverage (1.0 unless `--lot-coverage`), so a rule drawn at the terminal is always five edits; only the API and the page write a rule that leaves numbers out.

On the scenarios page (`/scenarios.html`) the same thing is one gesture: draw an area, type the numbers this area changes in the box that opens beside it (empty — a field left empty keeps each block's own value), Apply. Apply writes the rule, recomputes and switches the map to the change; on the baseline city it creates the scenario first. A saved rule is clicked to change its numbers, dragged or resized, or deleted, each followed by a recompute. When the recompute refuses (a per-block ceiling) or the rule catches no block, the page undoes its own write.

`recompute` materialises the edits into override rows (selecting the AOI's blocks — members in `aoi_feature` — with `ST_Covers(area, geometry_centroid)`; later ordinals win where areas overlap; a per-block ceiling breach refuses the whole materialisation) and then runs the selective recompute. From the API the run covers the scenario's **footprint** only — every block a current rule overrides, plus every block whose override this materialisation removed, with their attributes — rather than every block of the AOI: a block without an override reads the baseline through the overlay, so recomputing it under the scenario only rewrote baseline values (on Mexicali, 19,058 blocks for a rule over 2,400; about 20 s a run). The API passes that footprint to `submit_run` as an explicit change-set (`MaterializeResult.footprint_attributes` → `changed_attributes`, `footprint_features` → `edited_features`). The CLI's `scenario recompute` still plans from freshness; `scripts/acceptance_run.sh` step 10 exercises that CLI path (`scenario recompute --sync`).

---

### Recording a scenario edit directly

The low-level path writes override rows without a scenario row. It remains for code that already holds a scenario id; the authoring path above is the one that creates a scenario others can find by name.

```python
from uuid import uuid4
from reimagina.scenarios.overrides import apply_override

scenario_id = str(uuid4())
apply_override(
    conn,
    scenario_id=scenario_id,
    feature_id=block_id,
    attribute_key="floors",
    value_numeric=10.0,
)
```

`apply_override` upserts on `(scenario_id, feature_id, attribute_key)`, so re-recording an edit replaces it rather than failing (`overrides.py:52-54`), and publishes a new version of that attribute's override asset for the scenario unless `publish=False` (`apply_overrides` batches with it and publishes once per attribute).

Three surfaces author an edit, and none of them owns a rule. `apps/cli/scenario.py` at a terminal, `apps/api/routers/scenarios.py` over HTTP, and the scenario workspace in the browser (`apps/web/scenarios.html`; its pure rules in `apps/web/src/scenario.ts`, its page in `apps/web/src/scenarios/`) all reach `reimagina.scenarios.proposal` (the workspace through the API), which is where the edits a rule writes (`edits_for`, one per number it sets), the refusal of a degenerate number (`require_positive`, `require_coverage`), the ceiling precheck (`precheck_ceilings`) and the unioning of a drawn area (`polygon_wkt`) live — so the three refuse the same things for the same reasons. A surface adds exactly two things of its own: the dialect of its refusals (`--levels` on a command line, `levels` in a request body, a highlighted input on a form) and, in the workspace, the one check a form can make before the round trip.

**The workspace's precheck is a saved round trip, not a guarantee**, and the rule form says so next to its inputs (`CLIENT_PRECHECK_NOTE` in `scenario.ts`). Only the density and storey ceilings are independent of block area, so they are the only ones checkable before a block is chosen; the housing-unit and population ceilings are the server's, per block, at materialisation. The bounds themselves are read from `GET /indicators/edificability_block_density` and `GET /indicators/urban_form_block_planned_floors` (each one's `valid_max`) rather than compiled into the frontend, because `ceilings._bound_for` reads them from the same catalog — a second copy would agree until the day the catalog moved.

**The workspace shows only what a rule can change.** A rule writes up to five block attributes (`proposal.edits_for`), and `GET /scenarios/indicators` returns the indicators they reach — `proposal.reachable_indicators`, which asks `impact.compute_dirty_set`, the function that decides what a recompute runs. It asks with `floors`, `num_levels`, `unit_size` and `people_per_household`; `lot_coverage` is read only by the edificability family, which those already reach. Today that is five: planned floors, potential housing units, potential population, housing density, and the livability score through density. The other seven cannot move under any scenario, and the workspace does not offer them. Each row also says which number a summary table shows (`table_statistic`): a total for counts, a median for rates. **The baseline those indicators start from** is fitted per block from the Overture buildings and the 2020 census, so each input is the physical quantity the rule form names (`scripts/seed_demo_attributes.py --fit-to-census`): storeys are the footprint-weighted mean Overture `num_floors`, untagged buildings counting one (`overture block-storeys` writes it as a `floors` observation where a building is tagged; the fit copies it into `num_levels`, and 1 elsewhere); lot coverage is the building footprint clipped to the block ÷ block area; m² per dwelling is built floor area (footprint × storeys) ÷ census dwellings (`vivtot`); people per household is census population ÷ census dwellings (`pobtot ÷ vivtot`). With those inputs the formula gives every block's census dwellings and population back: 340,169 dwellings over Mexicali. ⚠ People per household is per dwelling, *occupied or not*: INEGI's `prom_ocup` divides by occupied dwellings only, and multiplying it by all dwellings put the baseline 13.8% above the census population. A rule's value is read the same way. ⚠ A block the census counts no dwellings on gets no m² per dwelling, so the family skips it; a rule that sets one there builds on it. A block with dwellings but no Overture footprint takes the median coverage of the blocks that have both.

**The workspace carries the scenario in one place.** The selected scenario goes on the overlay's tile URL, the ramp's stats, the table's cells and the download URLs — and nowhere else, because the overlay is resolved in SQL (r043) rather than in the client. The map page (`index.html`) shows the baseline city only. ⚠ The baseline's tile URL is byte-identical to the one that existed before scenarios: MapLibre caches tiles by URL, so a parameter added to the baseline invalidates every cached tile of every indicator on every AOI. Both pages build it with `scenario.tileUrlFor`, which asserts that.

---

### Triggering scenario computation

Submit a run; when no change-set is given, it is planned from the scenario's override versions (freshness), or from its override rows on a database with no asset graph.

```bash
python -m apps.cli.app evaluate-scenario --scenario-id "$SCENARIO_ID" --changed-attribute floors
```

The CLI's flags are `--changed-attribute` (repeatable), `--feature` (repeatable), `--scenario-id`, `--aoi-id`, `--dsn`, `--code-version`, `--timeout` (seconds, default 600), plus the mutually exclusive mode flags `--detach` / `--sync` (`apps/cli/app.py:696-756`).

Over HTTP:

```http
POST /jobs
{"scenario_id": "…", "changed_attributes": ["floors"]}
→ 202 {"run_id": "…", "status": "queued", "deduplicated": false}
```

Omitting `changed_attributes` and `edited_features` is the supported shape when a `scenario_id` is given — the plan is derived from the scenario's overrides (`apps/api/routers/jobs.py:132-133`, `plan.py:190-204`).

A run whose dirty-set is empty comes back as `{"run_id": null, "status": "noop"}`: nothing was invalidated, no run row exists, and an id would 404 (`jobs.py:152-156`, `:179-186`).

See `JOB_LAYER.md` for run modes, run state, and cancellation.

---

### Listing a scenario's overrides

```sql
SELECT
  so.feature_id,
  f.name,
  f.type_slug,
  so.attribute_key,
  so.value_numeric,
  so.value_text
FROM curated_geo.scenario_override so
JOIN serving_geo.feature_v f ON f.id = so.feature_id
WHERE so.scenario_id = :scenario_id
ORDER BY f.type_slug, so.attribute_key, so.feature_id;
```

The same read, reduced to `(changed_attributes, edited_features)`, is what `scenario_change_set` returns for the planner on a database with no asset graph (`overrides.py:215`).

---

### Checking run status for a scenario

See `JOB_LAYER.md` — *The port* (`get_run`), *Progress* (the SSE stream), and *Cancellation*. Status is per run and per unit; there is no per-model-family status to poll.

---

### Reading all scenario results for an AOI

```sql
SELECT
  f.id,
  f.name,
  f.geometry,
  af.overlap_ratio,
  ci.indicator_slug,
  ci.indicator_name,
  ci.value_numeric,
  ci.quality_flag
FROM curated_geo.aoi_feature af
JOIN serving_geo.feature_v f            ON f.id  = af.feature_id
JOIN serving_geo.current_indicator_v ci ON ci.feature_id = f.id
                                       AND ci.scenario_id = :scenario_id
WHERE af.aoi_id    = :aoi_id
  AND f.type_slug  = 'block'
  AND af.overlap_basis = 'area'
ORDER BY ci.indicator_slug, f.id;
```

`current_indicator_v` filters to `status = 'current'` (`r016:327-350`), so this returns the live result set for that scenario and nothing superseded. It returns only the rows the scenario itself wrote — what it recomputed. The effective values (its row where it has one, the baseline's otherwise) come through the overlay: `GET /indicators/{slug}/values?scenario_id=`, the downloads, or `storage._utils.scenario_overlay` in your own query.

---

### Comparing a scenario against the baseline

Baseline results are the rows with `scenario_id IS NULL`, written by the same indicator code through the same overlay.

```sql
SELECT
  f.id,
  f.name,
  base.value_numeric                        AS baseline_value,
  scen.value_numeric                        AS scenario_value,
  scen.value_numeric - base.value_numeric   AS delta
FROM serving_geo.feature_v f
JOIN serving_geo.current_indicator_v base
  ON base.feature_id     = f.id
 AND base.indicator_slug = 'urban_form_block_planned_floors'
 AND base.scenario_id IS NULL
JOIN serving_geo.current_indicator_v scen
  ON scen.feature_id     = f.id
 AND scen.indicator_slug = 'urban_form_block_planned_floors'
 AND scen.scenario_id    = :scenario_id
WHERE f.type_slug = 'block';
```

Swap `base.scenario_id IS NULL` for a second `= :scenario_b_id` to compare two scenarios. The inner joins keep only features both sides wrote, so this misses every block the scenario did not recompute and every indicator with no baseline row (the edificability family, wherever its baseline has not been computed). `GET /scenarios/{id}/comparison` (`scenarios/comparison.py`) is the complete form: a `FULL OUTER JOIN`, the scenario side read as `COALESCE(scenario, baseline)`, and each row classified `changed`, `unchanged` or `new`. Any of the twelve slugs in `SHIPPED_INDICATORS` (`indicators/catalog.py:709`) can be compared.

---

### Updating an override and recomputing

```sql
INSERT INTO curated_geo.scenario_override
  (scenario_id, feature_id, attribute_key, value_numeric)
VALUES (:scenario_id, :block_id, 'floors', 12.0)
ON CONFLICT (scenario_id, feature_id, attribute_key)
DO UPDATE SET value_numeric = EXCLUDED.value_numeric;
```

A raw `INSERT` publishes no override version, so a submit naming only the `scenario_id` plans from freshness and does not see this edit. Either write through `apply_override` (which publishes) or name the change-set: `{"scenario_id": …, "changed_attributes": ["floors"]}`. There is no separate "mark stale" step to perform: the write path stales the previous `current` row for the same `(feature, scenario, indicator)` as it inserts, and the partial unique index `ir_one_current_scenario` enforces one current row per triple (`r016:198-200`).

---

### Duplicating a scenario

A named scenario is its **edits**; its override rows are derived from them. Clone the edits, not the overrides, and let materialisation write the rest.

⚠ Copying only `scenario_override` rows looks complete and is not. The clone shows no drawn areas and cannot be edited, and the first `scenario recompute` on it deletes every copied override, because materialisation step 4 removes any override that no surviving edit implies. A clone with no edits implies none.

1. Create the scenario through `create_scenario` (or `scenario create`, or `POST /scenarios`) rather than inserting the row. That call is the only place an AOI is checked against the project, a tie the schema does not enforce (see r048).
2. Copy the edits, keeping their ordinals so the clone resolves overlapping areas in the same order:

   ```sql
   INSERT INTO curated_geo.scenario_edit
     (scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric)
   SELECT :new_scenario_id, ordinal, geometry, type_id, attribute_key, value_numeric
   FROM curated_geo.scenario_edit
   WHERE scenario_id = :source_scenario_id;
   ```

3. Recompute the new id. Materialisation writes its overrides and publishes its versions, and the run that follows computes its results. Until then the clone has edits but no overrides and no results, which `explore.scenario_v` shows as an `override_count` of 0.

A bare override set with no `app_core.scenario` row is still a valid what-if for the engine, as described above, but it is invisible to `explore.scenario_v` and to every authoring surface.

There is no archive operation and no status column. Deleting a scenario deletes every row keyed by it (see *The scenario id* above): overrides, results in any status, their job runs, and its partition's asset versions and materialisations. Nothing is kept as history, because nothing could read it once the scenario row is gone.

---

## Build order

Within this layer:

1. `app_core.project` — r001, needed by `area_of_interest`
2. `curated_geo.feature` — DATA_LAYER (r003); `scenario_override.feature_id` references it
3. `curated_geo.scenario_override` — r030
4. `app_core.scenario` and `curated_geo.scenario_edit` — r048

`scenario_override` is created late (r030) rather than with `project`, because it depends on `feature` and nothing depends on it. See `JOB_LAYER.md` — *Migration order* for how the job-layer tables interleave.

---

## How the layers relate

```
DATA_LAYER                      SCENARIOS_LAYER            JOB_LAYER              INDICATORS_LAYER

curated_geo.feature ─────────┐
curated_geo.observation ─────┼──► scenario_override ──► submit_run ──► job_run ──► indicator_result
        │                    │            │                 │                       (scenario_id NULL
        │                    └────────────┴── COALESCE ──────┤                        or a uuid)
        └──► serving_geo.feature_v                           └── runner resolves effective values
                (map/API reads)                                  writes results citing job_run_id
```

`feature` is the shared anchor. Scenarios contribute only override rows. A run resolves the three-way overlay in SQL at read time and writes its effects to `indicator_result`. The baseline is never modified.

Note the two distinct read paths: the API and tiles read features through `serving_geo.feature_v`, which merges base attributes with current observations (`r027_feature_centroid_index.py:82-101`) but knows nothing about scenarios. Indicator computation does not go through that view — it resolves `override ⊕ observation ⊕ base` itself (`effective_numeric`, `overrides.py:169-212`).
