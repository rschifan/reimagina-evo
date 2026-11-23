# Scenarios Layer

How the platform records a proposed change to the city, and how that record turns into recomputed indicator results.

**Related documents:** `DATA_LAYER.md` — the city data scenarios read from (never modified). `INDICATORS_LAYER.md` — the computed outputs scenarios produce. `JOB_LAYER.md` — the `Orchestrator` port that executes a scenario run.

---

## What this layer does

A scenario is a named "what if" — a proposed state of the city that has not happened yet. It overlays changes on the baseline city without ever modifying it.

The workflow is:

```
project  →  scenario  →  scenario_rule (drawn areas)  →  scenario_override rows  →  submit_run  →  indicator_result
```

- **Project** — `app_core.project`, the workspace an area of interest and its scenarios belong to (r001).
- **Scenario** — a named row in `app_core.scenario` (r048), unique by name within its project and bound to an AOI. It is the addressed resource: created, listed, shown, recomputed, compared and deleted by id. The `scenario_id` columns on `scenario_override`, `indicator_result`, `job_run` and `aoi_indicator_summary` reference it `ON DELETE CASCADE` (r055) — see *The scenario id* below.
- **Rule** — one drawn area and the numbers a planner set over it, a row of `curated_geo.scenario_rule` (r055) with its own id and its own place in "later wins". This is what a person authors.
- **Override** — one per-feature attribute value, in `curated_geo.scenario_override` (r030). Materialising a scenario expands its rules into override rows, and those rows are what the indicators read.
- **Run** — an execution submitted through the `Orchestrator` port. Defined in `JOB_LAYER.md`.
- **Indicator result** — the output, in `curated_geo.indicator_result` with `scenario_id` set (see `INDICATORS_LAYER.md`).

**The baseline is never touched.** An override is a *read-time overlay resolved in SQL*, not an edit to `feature` or `observation` and not an in-memory merge inside a worker. The effective value of an attribute is `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins (`scenarios.overrides.effective_numeric`).

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

⚠ The previous version of this document described this layer in terms of `app_core.intervention`, `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. None of those tables exist and none were ever created. The mechanism is `curated_geo.scenario_rule` (r048's `scenario_edit` until r055) expanded into `curated_geo.scenario_override`, with `app_core.scenario` naming the whole.

---

## Tables

### app_core.project

Top-level workspace. Groups areas of interest.

```sql
-- r001_schemas_and_extensions.py
CREATE TABLE IF NOT EXISTS app_core.project (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text        NOT NULL,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
```

It exists because `curated_geo.area_of_interest.project_id` references it (r004), and since r048 `app_core.scenario.project_id` does too.

---

### app_core.scenario

A named what-if inside a project (r048). `project_id` is required; `aoi_id` is nullable in the schema (it empties itself when the AOI is deleted) but every surface requires it at creation — see *The scenario id* below. `UNIQUE (project_id, name)` is what lets a scenario be addressed by name. `materialized_at` is null until the drawn rules have first been expanded into overrides. Row-level security is enabled on this table, keyed on `project_id`, but it is **inert today**: the policy admits every row while `app.current_project_id` is unset, nothing outside tests sets it, and the API connects as the table owner, so it provides no tenant isolation yet (a known, deferred risk).

### curated_geo.scenario_rule

One drawn area and the numbers set over it (r055): `id`, `scenario_id` (FK, `ON DELETE CASCADE`), `ordinal`, `type_id`, `geometry`, `inputs` (jsonb), `mode`, `units` (r058), `created_at`, `updated_at`.

- **`mode`** is `set` or `change` (r058; existing rules are `set`). A set rule's numbers are the blocks' new values. A change rule's numbers move each block from its **own baseline** value (observation ⊕ base, never an earlier rule's value), as an amount or a percentage per `units` (`abs`|`pct`, one per number, `abs` when missing; lot coverage `abs` is a share, 0.05 = +5 points). `_resolve_rules` computes `base + n` or `base × (1 + n/100)` (`proposal.changed_value`), held in the number's range (storeys ≥ 1, m² per dwelling ≥ 15, people per household 0.5–15, coverage 0.01–1, since a set rule refuses 0 coverage) and rounded to 4 decimals. A block without a baseline for an attribute gets no value for it, except coverage, whose absent baseline is the whole block (1.0), as the compute reads it; a change rule that changes no block at all is refused (`ChangeHasNoBaseline`, 422 `change_has_no_baseline`). A ceiling refusal names the rule and the largest change that fits every breaching block, in the rule's own terms (`+24 %`, `+8 storeys`). An update that names no `mode` keeps the rule's own. Later rules still win per attribute, so a change rule replaces an earlier rule's value on the blocks they share; rules never stack. `check_inputs` takes signed, non-zero changes and a percentage above −100; the ceilings are checked per block at materialisation, as for every rule. Overrides hold the resulting absolute values, so tiles, comparison and freshness read them as before; a baseline reload reaches a change rule at the scenario's next recompute.

- **`inputs`** holds the planner's numbers by name — `levels`, `unit_size`, `people_per_household`, `lot_coverage` — and nothing for a number left out, so every block keeps its own value for it. At least one is set (a CHECK refuses `{}`). The names, the attributes each writes and each one's range are one registry, `proposal.RULE_INPUTS`; `proposal.check_inputs` refuses an unknown name, a non-number, a non-finite value, a value outside its range (levels > 0; unit size more than 15 m² per dwelling; people per household in (0, 15]; coverage in (0, 1]) and the area-independent ceilings, on every surface. Writing all four numbers, as rules once did, flattened the area: a rule meant to change household size also gave every block the same storeys, unit size and coverage, and so the same density (`coverage × levels × 10 000 / unit size`, block area cancelling).
- **Storeys are one number.** A rule stores `levels` once; materialisation writes it to both `floors` and `num_levels` (`proposal.edits_for`), the names the two indicator families read, so the map cannot contradict itself.
- **`ordinal`** is the rule's place in "later wins". It is assigned when the rule is created (`max + 1` under the scenario's row lock) and never changes after: editing a rule's numbers or moving it keeps its place, so dropping a number and typing it again does not move the rule ahead of a later one. Not `created_at`, because `now()` is transaction-start time.
- **The geometry** is repaired on write (`_repair_geometry`: `ST_Force2D`, `ST_MakeValid`, then its polygonal part) and refused as `InvalidGeometry` when nothing polygonal is left. `proposal.polygon_wkt` has already dropped a third coordinate and refused vertices that are not longitude/latitude degrees.

**A rule the server would refuse is never stored.** Writing a rule (`add_rule`, `update_rule`, `delete_rule`) and materialising the scenario happen in one transaction (`materialize_for_rule`): a ceiling breach on any block, or a rule whose area catches no block of the AOI (`RuleCatchesNothing` — over the sea, outside the AOI, longitude and latitude swapped near the equator), rolls the write back. There is therefore no rule state to track and no client-side undo. `POST /scenarios` with a `rule` extends that to the scenario: `create_scenario` and `add_rule` share the transaction, so a refused first rule leaves no empty scenario for a client to delete.

r055 converted r048's `scenario_edit` rows into rules, grouped as the workspace grouped them (scenario, type, identical geometry): the rule's ordinal is the rank of its first edit and each number the one its latest edit carried. Its downgrade expands rules back into edits.

**Lot coverage.** `lot_coverage` is the share of the block that is built on (COS, `0 < c ≤ 1`). The edificability family computes `housing_units = area × lot_coverage × num_levels / unit_size`; a block without `lot_coverage` is computed at 1.0, the whole block — the collaborators' formula exactly. The fitted baseline carries each block's real coverage (Overture footprint ÷ block area), so a rule's coverage is compared with what is built today. Every surface writes it only when it is set. The ceiling checks (precheck and materialisation) count it.

### The scenario id

`curated_geo.scenario_override`, `curated_geo.indicator_result`, `jobs_runtime.job_run` and `curated_geo.aoi_indicator_summary` reference `app_core.scenario(id)` `ON DELETE CASCADE` (r055; r016 and r030 had left them bare uuids while the entity was deferred). `NULL` means baseline — the city as it is today. r055 deleted the rows that named no scenario before adding the constraints.

- A made-up uuid is refused: a scenario row must exist before anything is written under its id.
- ⚠ **A scenario's AOI is tied to its project by code, not by the schema.** r048 declares `aoi_id` as a plain reference to `curated_geo.area_of_interest(id)`, so the database accepts a scenario in project A bounded by project B's AOI. Materialisation scopes its selection by `aoi_id` alone, so such a scenario would write overrides over B's features. `create_scenario` is the column's only writer and refuses the pair (`_require_aoi_in_project`); a new insert path has to repeat that check. Every surface requires `aoi_id` at creation: without one a scenario can hold rules and never materialise them, and nothing sets it later (its rules are drawn over that AOI's blocks). It can still become null when the AOI is deleted (`ON DELETE SET NULL`), and materialisation then refuses with `ScenarioHasNoAOI`.
- `delete_scenario` locks the scenario row `FOR UPDATE` and deletes, in one transaction, the scenario's overrides, every `asset_version` and `asset_materialization` row of its partition (keyed by the string `scenario=<id>`, which no FK can follow), its `indicator_result`, `job_run` and `aoi_indicator_summary` rows (explicitly first, so each count is its own table's; the FKs would cascade them anyway), its finished recompute runs (`orchestration_run`, cascading their `job_unit` rows), then the row, which cascades its rules. A run still in flight stays, as a cancelled run in the run history: the API cancels the scenario's unfinished runs after the delete commits.
- **A compute unit cannot outlive the delete.** Each unit of a scenario run takes, before it computes, a transaction advisory lock on (scenario, indicator), and defers the FKs its job run and results carry to the scenario (they are `DEFERRABLE`), so computing locks nothing on the scenario row and a delete returns at once. Only after the compute does the unit take `FOR KEY SHARE` on the row, held until it commits (`runners._fenced`): a delete arriving then waits a moment and removes what the unit wrote; if the row is already gone the unit rolls back everything it wrote and ends `cancelled`, and a unit that starts after the delete finds no row and ends `cancelled` too. The same advisory lock serialises two runs of one scenario per indicator, so they no longer collide on `ir_one_current_scenario`, and the last to commit has read the latest overrides. Writers of rules lock the scenario `FOR NO KEY UPDATE`, which `KEY SHARE` does not block, so drawing a rule does not wait for a compute.
- Deleting a project cascades its scenarios and, through them, everything above except the partition-keyed versions and materialisations (no project-delete path exists today).

---

### curated_geo.scenario_override

One attribute value for one feature under one scenario, the resolution of its rules. Keyed like `observation`'s one-current-per-`(feature, attribute)` shape. Since r055 `scenario_id` is an FK to `app_core.scenario`, `ON DELETE CASCADE`.

```sql
-- r030_scenario_override.py
CREATE TABLE curated_geo.scenario_override (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid        NOT NULL,   -- FK to app_core.scenario since r055
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

Defined in `JOB_LAYER.md`. Note that it is **not** created at submit time by this layer — the runner opens it when the compute starts (the `job_run` context manager in `jobs/_lifecycle.py`).

---

## How a scenario edit becomes recomputed results

```
1. Draw a rule
     add_rule(conn, scenario_id=…, geometry_wkt=…, type_id=…,
              inputs={"levels": 10})                   authoring.add_rule
     → check_inputs, then INSERT INTO curated_geo.scenario_rule
     → materialize_scenario, in the same transaction  authoring.materialize_scenario
       -- the rules resolve to per-block edits (proposal.edits_for)
     → apply_overrides: one upsert into scenario_override  overrides.apply_overrides
     → publish_scenario_version(conn, scenario_id)    overrides.publish_scenario_version
       -- a new version of scenario.override.floors (and num_levels) at this
       -- scenario's partition; this is what freshness sees

2. Submit a run
     orchestrator.submit_run(conn, changed_attributes=…, edited_features=…,
                             scenario_id=…)                        JOB_LAYER.md

3. Plan it                                                         plan.plan_run
     -- An explicit change-set wins: compute_dirty_set over it.
     -- Nothing named and an asset graph present: the dirty set comes from
     --   freshness at this scenario's partition (the versions step 1 published).
     -- Nothing named and no asset graph: resolve_change_set reads the diff
     --   back from the scenario's override rows (overrides.resolve_change_set) — the
     --   rows are the diff, so a caller need not re-declare what changed.

4. Run the dirty set                                               JOB_LAYER.md
     -- compute_dirty_set: every indicator whose declared `reads` intersects
     -- the changed attributes, plus everything transitively depending on it.

5. Each runner reads effective values, not baseline values
     effective_numeric(conn, type_id=…, attribute_key="floors",
                       scenario_id=…, feature_ids=…)               overrides.effective_numeric

6. Results are written to curated_geo.indicator_result with the run's scenario_id
```

Step 3 is why an override edits `attribute_key` rather than some scenario-specific vocabulary: the key *is* the `changed_attributes` token the impact engine keys the dirty-set off, so no translation layer is needed (`scenarios/overrides.py` module docstring).

**Attribute scoping depends on the path, and so does feature scoping.** A scenario submit that names nothing goes through the asset graph, where there is one override asset per overridable attribute (`assets/registry.py`, `scenario_override_assets`). Editing `floors` publishes a version for `scenario.override.floors` alone, so only the indicators declaring an edge to `floors` — and whatever is downstream of them — are recomputed. Editing an attribute no indicator reads recomputes nothing. That path is whole-indicator in *features*: every unit it plans covers every feature the indicator applies to (`plan._units_from_freshness`).

An explicit change-set — `POST /jobs` with `changed_attributes` / `edited_features`, and `POST /scenarios/{id}/recompute`, which passes the materialisation's footprint — goes through `impact.compute_dirty_set`. Its attribute half is a flat union: every indicator reading *any* named attribute is recomputed over *every* named feature. Its feature half is scoped: a seed indicator gets the named features and a dependent the union of its inputs' features, which the runner passes on as `feature_ids` (`runners._scoped`). With no asset graph the scenario-only submit takes this path too, with `resolve_change_set`'s union of the override rows. Widening or remapping a feature set per indicator (`impact.FeatureExpander`) is a hook with only the identity implementation. With a `scenario_id` the graph still has a say: an indicator already computed for that scenario and AOI at its current version is dropped, and a run over the whole footprint is recorded as covering the AOI (`runners._recording`), so recomputing an unchanged scenario is a noop.

---

## Effective attribute resolution

The overlay is one `COALESCE` over three sources, evaluated in the database (`overrides.effective_numeric`):

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

Only features whose effective value is non-null are returned (`overrides.effective_numeric`). Postgres only — the function rejects any other backend (`overrides._require_postgres`).

`compute_planned_floors` is the worked example: it reads the effective `floors` per block through this function and writes the result inside a `job_run` context (`indicators/planned_floors.py`).

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

`apps/api/routers/scenarios.py`, mounted at `/scenarios`. Every handler opens its own committed connection (`contextlib.closing(connect(api_dsn()))`), never the pooled `get_db_conn`, which rolls back on success. A malformed scenario id in the path answers 404, as an unknown one does; a malformed uuid in a query or body field answers 422. The API's CORS policy allows `PATCH` alongside `GET`, `POST`, `DELETE` and `OPTIONS` (`apps/api/app.py`), so `PATCH …/rules/{rule_id}` also works from a cross-origin frontend. A malformed rule id in the path answers 404, as a scenario id does.

| Method | Path | Body / query | Success | Refusals |
|---|---|---|---|---|
| GET | `/scenarios` | `project_id`, `aoi_id` (both optional) | 200: rows newest first, each with `rule_count` and `latest_run` (`{id, status}` or `null`) | — |
| POST | `/scenarios` | `project_id`, `aoi_id`, `name` (non-empty), `description`?, `rule`? (`area`, `inputs`, `type_context`, `type_slug`, as `POST …/rules`) | 201: the scenario as `GET` returns it, plus `first_rule` (as `POST …/rules` answers, or `null` without a `rule`) | 422 no `aoi_id` or an empty `name`; 404 `unknown_reference` (`field: aoi_id`) for an AOI that does not exist; 404 (a sentence) for an AOI not in the named project, which is also the answer when the project does not exist; 409 a duplicate name in the project; with a `rule`, the refusals of `POST …/rules`, and a refused rule leaves no scenario |
| GET | `/scenarios/indicators` | — | 200: the indicators a rule can change, inputs first, each with `statistic` (`sum` for `units` and `people`, else `median`) | — |
| GET | `/scenarios/{scenario_id}` | — | 200: the scenario, `override_count`, and `rules` (`id`, `ordinal`, `type_id`, `inputs`, `geometry_wkt`, `created_at`, `updated_at`) in ordinal order | 404 |
| GET | `/scenarios/{scenario_id}/rules` | — | 200: the rules, as above | 404 |
| DELETE | `/scenarios/{scenario_id}` | — | 200: `{scenario_id, overrides, versions_deleted, results_deleted, job_runs_deleted, materializations, runs_deleted}`; the scenario's unfinished runs are then cancelled | 404, also on a second delete; a run cancelled after some of its units finished ends `partial` |
| POST | `/scenarios/{scenario_id}/rules` | `area` (GeoJSON FeatureCollection, Feature or geometry; polygons unioned), `inputs` (any of `levels`, `unit_size`, `people_per_household`, `lot_coverage`; omitted or null is not set), `type_context` (`MEX`), `type_slug` (`block`) | 201: the recompute shape below plus `rule`; the rule is written and the scenario materialised in one transaction, then the recompute is enqueued | 400 `empty_proposal` (`field: null`; also when every number is null), `invalid_value` (`field` names the number: unknown name, not a number, non-finite, or out of range), `invalid_geometry` (`field: area`; no polygon, or not lon/lat degrees); 422 `ceiling_precheck_failed` with `levels`, `unit_size` (0 when the rule sets none) and `allowed_levels`; 422 `ceiling_exceeded` (as recompute); 422 `rule_catches_nothing` (`field: area`); 422 `change_has_no_baseline` (`field` names the number); 404 unknown scenario or feature type; 409 the scenario has no AOI; nothing is stored on any refusal; 500 `recompute_not_enqueued` (the rule stands) |
| PATCH | `/scenarios/{scenario_id}/rules/{rule_id}` | `inputs` (the rule's whole set: a number left out is no longer set), `area`? (omitted keeps it) | 200: as `POST …/rules`; the rule keeps its id and its ordinal | as `POST …/rules`, plus 404 a rule not of this scenario; a refused change leaves the rule as it was |
| DELETE | `/scenarios/{scenario_id}/rules/{rule_id}` | — | 200: as `POST …/rules` with `rule: null`; no run is enqueued when no override is left | 404 a rule not of this scenario; 409 the scenario has no AOI; 422 `ceiling_exceeded` when what is left breaches (an earlier rule now wins) |
| POST | `/scenarios/{scenario_id}/recompute` | — | 202: `{scenario_id, matched_per_rule, overrides_written, overrides_removed, results_superseded, run_id, run_status, deduplicated}`; `run_id` is `null` on a noop, unless a run of the scenario is still going, which is then answered with `deduplicated: true` | 404; 409 the scenario has no AOI (it was deleted); 422 `ceiling_exceeded` with `message`, `breach_count` and up to 20 `breaches`, each with `feature_id`, `indicator_slug`, `bound`, `would_be`, `rule_ids`, `rule_numbers` (the "Rule N" a planner sees: the rule's rank in ordinal order) and `allowed_levels`; 500 `recompute_not_enqueued` (materialised and committed, enqueue refused — retry is safe); 503 `ops_plane_not_provisioned` or `database_unavailable` |
| GET | `/scenarios/{scenario_id}/comparison` | `indicator_slug`, `aoi_id`?, `limit`? (≥ 1; omitted returns every row) | 200: `{scenario_id, indicator_slug, aoi_id, summary: {changed, unchanged, new, mean_delta, higher_is_better}, row_count, rows: [{feature_id, baseline, scenario, delta, state}], rows_truncated}` | 404 unknown scenario, indicator or AOI; 422 `limit` < 1 or a malformed `aoi_id` |

Writing a rule is two transactions, like `recompute`: the rule and the materialisation commit together, and the run is enqueued after, so the scenario's row lock is not held through the compute. The run covers the **footprint** — the blocks that carry an override, with the attributes involved — and an empty footprint enqueues nothing (an empty feature list would mean "every block" to the planner).

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
python -m apps.cli.app scenario add-rule --scenario-id <uuid> \
    --geojson area.geojson [--levels 12] [--unit-size 80] \
    [--people-per-household 3.2] [--lot-coverage 0.6]
python -m apps.cli.app scenario rules --scenario-id <uuid>
python -m apps.cli.app scenario update-rule --scenario-id <uuid> --rule-id <uuid> \
    [--geojson moved.geojson] [--levels 8] [--unset lot-coverage] …
python -m apps.cli.app scenario delete-rule --scenario-id <uuid> --rule-id <uuid>
python -m apps.cli.app scenario recompute --scenario-id <uuid> [--sync | --detach]
python -m apps.cli.app scenario compare --scenario-id <uuid> \
    --slug urban_form_block_planned_floors
python -m apps.cli.app scenario list      # reads explore.scenario_v
python -m apps.cli.app scenario show --scenario-id <uuid>
python -m apps.cli.app scenario delete --scenario-id <uuid>
```

The CLI writes a rule exactly as the API does: only the numbers given (no default coverage), checked by `proposal.check_inputs`, and materialised in the transaction that writes it, so a refused rule is not stored. `update-rule` merges: the numbers given replace the rule's own, `--unset NAME` (repeatable) removes one, the rest are kept, and `--geojson` alone moves the rule keeping its numbers. The API's `PATCH …/rules/{rule_id}` instead takes the rule's whole set (a number left out is no longer set) — the page sends it from a form that holds every number. `recompute` submits the materialisation's footprint, scoped to the AOI the materialisation read under its lock, as `POST …/recompute` does; `scripts/acceptance_run.sh` step 10 exercises it (`scenario recompute --sync`).

On the scenarios page (`/scenarios.html`) the same thing is one gesture: draw an area, type the numbers this area changes in the box that opens beside it (empty — a field left empty keeps each block's own value), Apply. Apply writes the rule — which materialises it and enqueues its recompute — and switches the map to the change; on the baseline city it creates the scenario with the rule, in one request (`POST /scenarios` with `rule`). A saved rule is opened from the map or from the list of rules beside it, to change its numbers, drag or resize it, or delete it. A refusal stores nothing, so the page has nothing to undo; when only the enqueue failed, the rule stands and the scenario's row offers a Recompute.

Materialisation selects the AOI's blocks — members in `aoi_feature` — with `ST_Covers(area, geometry_centroid)`; later rules win where areas overlap, per number; a per-block ceiling breach refuses the whole materialisation. A block a rule has left loses every scenario row (it reads the baseline through the overlay), so recomputing never writes copies of the baseline under the scenario that would later shadow a baseline change; and the freshness records of the indicators a removed number fed are dropped, so re-adding a rule (whose override version, a content digest, comes back) recomputes rather than being called fresh over the retired rows.

Three surfaces author a rule, and the rule is the server's. `apps/cli/scenario.py` at a terminal, `apps/api/routers/scenarios.py` over HTTP, and the scenario workspace in the browser (`apps/web/scenarios.html`; its pure rules in `apps/web/src/scenario.ts`, its page in `apps/web/src/scenarios/`) all reach `reimagina.scenarios.authoring` and `reimagina.scenarios.proposal` (the workspace through the API), which is where the registry of a rule's numbers (`RULE_INPUTS`), the edits a rule writes (`edits_for`), the refusal of a degenerate or implausible number (`check_inputs`), the ceiling precheck (`precheck_ceilings`) and the unioning of a drawn area (`polygon_wkt`) live — so the three refuse the same things for the same reasons. A surface adds exactly two things of its own: the dialect of its refusals (`--levels` on a command line, `levels` in a request body, a highlighted input on a form) and, in the workspace, the one check a form can make before the round trip.

**The workspace's precheck is a saved round trip, not a guarantee**, and the rule form says so next to its inputs (`CLIENT_PRECHECK_NOTE` in `scenario.ts`). Only the density and storey ceilings are independent of block area, so they are the only ones checkable before a block is chosen; the housing-unit and population ceilings are the server's, per block, at materialisation. The bounds themselves are read from `GET /indicators/edificability_block_density` and `GET /indicators/urban_form_block_planned_floors` (each one's `valid_max`) rather than compiled into the frontend, because `ceilings._bound_for` reads them from the same catalog — a second copy would agree until the day the catalog moved.

**The workspace shows only what a rule can change.** A rule writes up to five block attributes (`proposal.edits_for`), and `GET /scenarios/indicators` returns the indicators they reach — `proposal.reachable_indicators`, which asks `impact.compute_dirty_set`, the function that decides what a recompute runs, with every attribute a rule can write. Today that is five: planned floors, potential housing units, potential population, housing density, and the livability score through density (scored against a 50–150 units/ha target band, so a rule that pushes a block past 150 lowers it; `INDICATORS_LAYER.md`). The other seven cannot move under any scenario, and the workspace does not offer them. Each row also says which number a summary table shows (`table_statistic`): a total for counts, a median for rates. **The baseline those indicators start from** is fitted per block from the Overture buildings and the 2020 census, so each input is the physical quantity the rule form names (`scripts/seed_demo_attributes.py --fit-to-census`): storeys are the footprint-weighted mean Overture `num_floors`, untagged buildings counting one (`overture block-storeys` writes it as a `floors` observation where a building is tagged; the fit copies it into `num_levels`, and 1 elsewhere); lot coverage is the union of the building footprints clipped to the block ÷ block area (overlapping footprints count once); m² per dwelling is built floor area (footprint × storeys) ÷ census dwellings (`vivtot`); people per household is census population ÷ census dwellings (`pobtot ÷ vivtot`). With those inputs the formula gives every block's census dwellings and population back: 340,169 dwellings over Mexicali. ⚠ People per household is per dwelling, *occupied or not*: INEGI's `prom_ocup` divides by occupied dwellings only, and multiplying it by all dwellings put the baseline 13.8% above the census population. A rule's value is read the same way. ⚠ A block the census counts no dwellings on gets no m² per dwelling, so the family skips it, and the livability score with it (no dwellings, no livability — by design, `INDICATORS_LAYER.md`); a rule that sets one there builds on it, and the block is then scored. A block with dwellings but no Overture footprint takes the median coverage of the blocks that have both, and so does a block whose own footprint leaves under 20 m² per dwelling (`MIN_UNIT_SIZE_M2`: Overture mapped only part of its buildings); the median is taken over the blocks that pass that floor. Both are marked `estimated` in the block's attributes (`no_footprint` / `thin_footprint`) — on Mexicali 1,994 and 103 blocks — and the census is still reproduced. The overlay tile carries the reason per block (r056), `GET /indicators/{slug}/stats` counts the rows on them (`count_estimated`), and the map's tooltip, inspector and legend say "estimated — buildings not fully mapped".

**The workspace carries the scenario in one place.** The selected scenario goes on the overlay's tile URL, the ramp's stats, the table's cells and the download URLs — and nowhere else, because the overlay is resolved in SQL (r043) rather than in the client. The map page (`index.html`) shows the baseline city only. ⚠ The baseline's tile URL is byte-identical to the one that existed before scenarios: MapLibre caches tiles by URL, so a parameter added to the baseline invalidates every cached tile of every indicator on every AOI. Both pages build it with `scenario.tileUrlFor`, which asserts that.

---

### Triggering scenario computation

Submit a run; when no change-set is given, it is planned from the scenario's override versions (freshness), or from its override rows on a database with no asset graph.

```bash
python -m apps.cli.app evaluate-scenario --scenario-id "$SCENARIO_ID" --changed-attribute floors
```

The CLI's flags are `--changed-attribute` (repeatable), `--feature` (repeatable), `--scenario-id`, `--aoi-id`, `--dsn`, `--code-version`, `--timeout` (seconds, default 600), plus the mutually exclusive mode flags `--detach` / `--sync` (`apps/cli/app.py`, `evaluate_scenario`).

Over HTTP:

```http
POST /jobs
{"scenario_id": "…", "changed_attributes": ["floors"]}
→ 202 {"run_id": "…", "status": "queued", "deduplicated": false}
```

Omitting `changed_attributes` and `edited_features` is the supported shape when a `scenario_id` is given — the plan is derived from the scenario's overrides (`apps/api/routers/jobs.py` `SubmitRunBody`, `plan.plan_run`).

A run whose dirty-set is empty comes back as `{"run_id": null, "status": "noop", "deduplicated": false}` (still 202): nothing was invalidated, no run row exists, and an id would 404 (`jobs.SubmitRunResponse`, `jobs._submit_response`).

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

The same read, reduced to `(changed_attributes, edited_features)`, is what `scenario_change_set` returns for the planner on a database with no asset graph (`overrides.scenario_change_set`).

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

`current_indicator_v` filters to `status = 'current'` (r016), so this returns the live result set for that scenario and nothing superseded. It returns only the rows the scenario itself wrote — what it recomputed. The effective values (its row where it has one, the baseline's otherwise) come through the overlay: `GET /indicators/{slug}/values?scenario_id=`, the downloads, or `storage._utils.scenario_overlay` in your own query.

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

Swap `base.scenario_id IS NULL` for a second `= :scenario_b_id` to compare two scenarios. The inner joins keep only features both sides wrote, so this misses every block the scenario did not recompute and every indicator with no baseline row (the edificability family, wherever its baseline has not been computed). `GET /scenarios/{id}/comparison` (`scenarios/comparison.py`) is the complete form: a `FULL OUTER JOIN`, the scenario side read as `COALESCE(scenario, baseline)`, and each row classified `changed`, `unchanged` or `new`. Any of the twelve slugs in `SHIPPED_INDICATORS` (`indicators/catalog.py`) can be compared.

---

### Updating an override and recomputing

```sql
INSERT INTO curated_geo.scenario_override
  (scenario_id, feature_id, attribute_key, value_numeric)
VALUES (:scenario_id, :block_id, 'floors', 12.0)
ON CONFLICT (scenario_id, feature_id, attribute_key)
DO UPDATE SET value_numeric = EXCLUDED.value_numeric;
```

A raw `INSERT` publishes no override version, so a submit naming only the `scenario_id` plans from freshness and does not see this edit. Either write through `apply_overrides` (which publishes) or name the change-set: `{"scenario_id": …, "changed_attributes": ["floors"]}`. There is no separate "mark stale" step to perform: the write path stales the previous `current` row for the same `(feature, scenario, indicator)` as it inserts, and the partial unique index `ir_one_current_scenario` enforces one current row per triple (r016).

---

### Duplicating a scenario

A named scenario is its **rules**; its override rows are derived from them. Clone the rules, not the overrides, and let materialisation write the rest.

⚠ Copying only `scenario_override` rows looks complete and is not. The clone shows no drawn areas and cannot be edited, and the first `scenario recompute` on it deletes every copied override, because materialisation step 4 removes any override that no surviving rule implies. A clone with no rules implies none.

1. Create the scenario through `create_scenario` (or `scenario create`, or `POST /scenarios`) rather than inserting the row. That call is the only place an AOI is checked against the project, a tie the schema does not enforce (see r048).
2. Copy the rules, keeping their ordinals so the clone resolves overlapping areas in the same order:

   ```sql
   INSERT INTO curated_geo.scenario_rule
     (scenario_id, ordinal, geometry, type_id, inputs)
   SELECT :new_scenario_id, ordinal, geometry, type_id, inputs
   FROM curated_geo.scenario_rule
   WHERE scenario_id = :source_scenario_id;
   ```

3. Recompute the new id. Materialisation writes its overrides and publishes its versions, and the run that follows computes its results. Until then the clone has rules but no overrides and no results, which `explore.scenario_v` shows as an `override_count` of 0.

A bare override set with no `app_core.scenario` row is still a valid what-if for the engine, as described above, but it is invisible to `explore.scenario_v` and to every authoring surface.

There is no archive operation and no status column. Deleting a scenario deletes every row keyed by it (see *The scenario id* above): overrides, results in any status, their job runs, and its partition's asset versions and materialisations. Nothing is kept as history, because nothing could read it once the scenario row is gone.

---

## Build order

Within this layer:

1. `app_core.project` — r001, needed by `area_of_interest`
2. `curated_geo.feature` — DATA_LAYER (r003); `scenario_override.feature_id` references it
3. `curated_geo.scenario_override` — r030
4. `app_core.scenario` — r048
5. `curated_geo.scenario_rule`, and the scenario id as an FK on overrides, results, job runs and AOI summaries — r055

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

Note the two distinct read paths: the API and tiles read features through `serving_geo.feature_v`, which merges base attributes with current observations (r027) but knows nothing about scenarios. Indicator computation does not go through that view — it resolves `override ⊕ observation ⊕ base` itself (`overrides.effective_numeric`).
