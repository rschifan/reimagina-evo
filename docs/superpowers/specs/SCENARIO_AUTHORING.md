# Design: scenario authoring, so a proposal can be made and measured

Give a scenario an identity and a way in. Today a scenario is a bare uuid
that can only be created by running Python, and the proposal it represents
exists nowhere — only the per-block rows it happened to write.

**Status:** design, reviewed. Written before the code.

---

## Intent, and why now

The contract is *Conceptualización y diseño de una centralidad urbana en
Mexicali* — the design of an urban centrality. The platform can describe the
city as it is. It cannot yet express a proposal about how it might be, which
is the thing the engagement is actually for.

The gap has become concrete. On real Mexicali data, **five of twelve
indicators write zero rows**: the three `edificability_block_*`,
`urban_form_block_planned_floors`, and `livability_block_score`, which
consumes the first group. They all fail for one reason — they read
`num_levels`, `unit_size`, `people_per_household` and `floors`, and nothing
supplies them.

That is not a data gap to be closed by ingesting more. It was tested:
Overture's buildings for Mexicali carry `num_floors` on **1,152 of 387,273**
footprints (0.30%) and `height` on 4.74%, median 3.77 m. Deriving block-level
storey counts from that would be inventing 99.7% of the answer. These are
*policy* parameters — how many levels may be built — and no survey carries
them because they are a choice, not an observation.

A scenario is where that choice legitimately lives. "Allow six levels in this
district" is an input a person supplies, and the five dead indicators are
exactly the ones that evaluate its consequences.

---

## Goals

- A scenario can be **named, listed and found again** tomorrow.
- A proposal is stored as **a rule over an area** — "six levels inside this
  polygon" — not as the four thousand per-block rows it produces.
- A proposal **re-applies after a re-ingest**: feature ids change, the
  geometric rule does not.
- Someone can **draw an area on the map, set the values, and see the
  indicators recompute** against the proposal.
- The five currently-empty indicators produce real values under a scenario.

## Non-goals

- **Scenario comparison.** Viewing two proposals side by side is a later
  piece; this one has to exist first.
- **Versioning or branching** of a scenario's history.
- **A zoning model.** No land-use classes, no setbacks, no FAR tables. A
  scenario sets the attributes the indicators already read, and no others.
- **Permissions beyond what exists.** See "RLS is cosmetic here" below. This
  work does not build the deferred project-context enforcement.
- **Export and import.** See "What this does not give you".
- **Changing what the indicators compute.** The formulas stay as they are;
  this supplies their inputs.

## Anti-goals — what would make this a failure even if it worked

- **A scenario you cannot find again.** If the answer to "which proposal was
  that?" is a uuid in someone's notes, this did not solve the problem it was
  built for.
- **Silent defaults.** If a scenario applies `num_levels = 6` because that is
  the seeded demo value, it manufactures a finding — the same failure as the
  park indicator reporting a 1,500 m cutoff on an empty destination set. Every
  value in a scenario must be an explicit human choice, and an unset attribute
  must leave the indicator empty rather than guess.
- **Losing the rule.** If the database holds only override rows, nobody can
  tell what was proposed, re-apply it, or explain it to a client.
- **Touching the baseline.** Non-negotiable, and already guaranteed by the
  overlay design; nothing here may weaken it.

---

## Constraints the builder must respect

These are load-bearing. Each is evidenced in the codebase.

| | |
|---|---|
| **The deferral is recorded** | `app_core.scenario` was deliberately deferred — `r016:7-10`, `r030:5-6`, `SCENARIOS_LAYER.md`, `TERMINOLOGY.md`. This design reverses it *partially and deliberately*; see "The FK question". |
| **RLS is cosmetic here** | `app_core.project` and `curated_geo.area_of_interest` carry a `tenant_isolation` policy on `app.current_project_id`, and **nothing in the application ever sets that GUC** — only tests do. `r011` is permissive when it is unset and its own comment defers `FORCE RLS` and a dedicated role "until the first API route is deployed"; `ENGINEERING_NOTES.md:112-120` lists what is still missing. The new table matches the existing pattern exactly and inherits its limits. Building the real enforcement is a separate project. |
| **Bulk writes only** | `apply_overrides` publishes once per attribute. Per-edit publishing is quadratic — the docstring measures upzoning 10,000 blocks at ~50 million row reads. Materialisation must call `apply_overrides`, never `apply_override` in a loop. |
| **Per-attribute versions** | `publish_scenario_version` versions `scenario.override.<attr>@scenario=S` separately (r045). This is what makes selective recompute exact: editing `floors` must not invalidate indicators that read `num_levels`. Do not collapse to one scenario-wide version. |
| **The read path is finished** | r043 resolves scenario-over-baseline as an **anti-join** across six readers and the overlay tile. ⚠ Never widen it to `scenario_id = X OR scenario_id IS NULL` — that double-counts every recomputed feature. |
| **Migration head is r047** | The new migration is r048. |

---

## Approaches considered

**A — Identity only.** Add `app_core.scenario` for the name and project link.
Drawing an area writes one override row per block inside it; the scenario *is*
its list of edits.
*Trade-off:* the smallest migration. But nothing records what rule produced
four thousand override rows — the edits are visible, the intent never is — and
a re-ingest that regenerates feature ids orphans every one of them.

**B — The scenario is a rule.** `app_core.scenario` plus
`curated_geo.scenario_edit`: an area, an attribute, a value. Override rows
stop being authored and become *derived* — materialising the edits produces
them.
*Trade-off:* two tables and a materialisation step rather than one table. In
exchange the proposal states its intent, re-applies after a re-ingest, and
maps one-to-one onto the UI: draw, set the values, save.

**C — No new tables.** An endpoint takes polygon + attribute + value, writes
overrides, returns the uuid. Honours the recorded deferral exactly.
*Trade-off:* fastest and argues with nothing, but scenarios stay unnameable
and unfindable — the current situation with a nicer front door.

**Chosen: B**, by Rossano.

⚠ **A correction to the argument that was originally made for it.** B was
first justified as surviving a data reload. That was wrong, and the builder
review caught it: `mexicali_run.sh` runs `DROP DATABASE`, which takes
`app_core.scenario` and `scenario_edit` with it exactly as surely as the
override rows. Storing the rule does not survive a drop. The claim is
narrowed, by decision, to what is true: **a rule re-applies after a
re-ingest** — under `--keep`, or against any rebuilt feature set, because it
selects by geometry rather than by feature id, and because it says what was
intended rather than only what was written.

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

`aoi_id` is nullable and `ON DELETE SET NULL`: a scenario is *about* an area
of interest, but deleting the AOI should orphan the proposal rather than
destroy it. `UNIQUE (project_id, name)` is what makes "find it again" work.

**Where `project_id` comes from** *(my call)*: derived from the AOI when
`--aoi-id` is given, which is every real case. Required explicitly otherwise.
There is no project on the request surface — no auth, no header, no query
param — so the API takes it in the body for now, exactly as the rest of the
platform does nothing with project context.

**RLS:** enable row security and add `tenant_isolation` keyed on `project_id`,
matching the other two tables. The integration test probes it by setting
`app.current_project_id` by hand, the way
`tests/integration/test_data_layer_contracts.py:686` already does. It is not a
claim that anything enforces isolation at runtime, because nothing does.

### `curated_geo.scenario_edit`

The rule. One row is "inside this area, set this attribute to this value".

```sql
CREATE TABLE curated_geo.scenario_edit (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  geometry      geometry(Polygon, 4326) NOT NULL,
  feature_type  text NOT NULL,          -- 'block' today; the grain the rule applies to
  attribute_key text NOT NULL,
  value_numeric double precision,
  value_text    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(value_numeric, value_text) = 1)
);
CREATE INDEX scenario_edit_scenario_idx ON curated_geo.scenario_edit (scenario_id);
CREATE INDEX scenario_edit_geometry_gist ON curated_geo.scenario_edit USING gist (geometry);
```

It lives in `curated_geo` rather than `app_core` because it carries geometry
and joins to `curated_geo.feature`; `app_core` holds no spatial data today.

### What a scenario must set

**A scenario sets all three policy inputs** — `num_levels`, `unit_size`,
`people_per_household` — over the area it draws.

This is forced by the compute, not a preference: `edificability.py:101-107`
skips any feature missing *any* of the three, so "six levels" alone still
writes zero rows. The anti-goals forbid defaulting the other two, so the
authoring surface asks for all of them together. One drawn area therefore
produces **four** edit rows (see the levels note below), not one, and the UI
presents them as a single act.

⚠ `people_per_household` is the one of the three that *could* have come from
the census — INEGI's `prom_ocup` is a measured field and `HEADLINE_COLUMNS`
takes new columns without code changes. It is authored here anyway, by
decision: mixing a measured value and two policy values inside one proposal
makes the result hard to read, and a scenario that says what it assumes is
worth more than one that half-infers. Supplying a census-derived default is a
good later change, and it belongs with a UI that shows the user what the
census says before they override it.

### "Levels" is one control and two keys

`floors` and `num_levels` are the same physical quantity under two names.
`seed_demo_attributes.py:23-40` says so, says both are written with the same
value, and says the split is deliberate because the two keys have different
invalidation semantics — `urban_form_block_planned_floors` reads `floors`, the
three edificability indicators read `num_levels`.

**One "levels" control writes both keys** *(decided by Rossano)*. The
alternative — two independent fields — ships a UI whose most obvious action
leaves `planned_floors` showing the baseline while the edificability family
moves, so the map contradicts itself about one proposal.

This is not the silent default the anti-goals forbid: the value written to
both keys is the value the user typed, and the UI says that "levels" sets
both. A default is a number nobody chose; this is one number stored in the two
places the schema keeps it.

### Materialisation

```
scenario_edit  --(materialize)-->  scenario_override  --(evaluate-scenario)-->  indicator_result
```

`materialize_scenario(conn, *, scenario_id)` in `reimagina.scenarios`:

1. For each edit, select features where `type_slug = edit.feature_type` and
   the feature is **covered by** the edit geometry, scoped to the scenario's
   AOI through `aoi_feature` when `aoi_id` is set.
2. Collect `(feature_id, attribute_key, value)` across all edits. **Later
   edits win** on collision, so an overlapping correction behaves the way a
   person drawing a second shape expects.
3. Delete override rows for this scenario that no edit implies any more — a
   removed edit must remove its effect, or a scenario accumulates ghosts.
4. Apply the rest in **one** `apply_overrides` call per run.
5. ⚠ **Reconcile versions with `publish_scenario_version(conn, scenario_id)`
   and no `attributes` argument.** `apply_overrides` publishes only the
   attributes it just touched, so an attribute whose last override row was
   deleted in step 3 keeps a current version forever and the indicator that
   reads it never goes stale. The unfiltered call is documented to reconcile
   exactly that case. *(Caught by the builder review; step 3 without step 5 is
   a bug.)*

It must be **idempotent**: materialising twice changes nothing and, because
`publish_scenario_version` digests content, publishes nothing — so a scenario
is not recomputed for being saved twice.

⚠ **`ST_CoveredBy`, not `ST_Intersects`.** A block straddling the boundary of
a drawn area is not "in" the proposal, and counting it makes the rule's effect
depend on where the user happened to release the mouse. `compute_overlaps`
already uses the covered-by/intersection split for the same reason.

### The FK question — how far the deferral is reversed

`scenario_edit.scenario_id` gets a real foreign key: both sides are new, so
there is nothing to backfill.

`scenario_override.scenario_id`, `indicator_result.scenario_id` and
`jobs_runtime.job_run.scenario_id` **keep their bare uuids**. Those columns
hold historical ids with no scenario row — the acceptance run mints one from
`uuid5` — and backfilling placeholder scenarios to satisfy a constraint would
invent records of proposals nobody made. The deferral is therefore reversed
for *authoring* and left in place for *results*, which is a smaller claim than
r016 anticipated and must be recorded as such in `SCENARIOS_LAYER.md`.

### Deleting a scenario *(my call)*

Delete is a **retirement, not a row delete**, in this order:

1. Delete the edits (FK cascade).
2. Delete this scenario's override rows.
3. `publish_scenario_version(conn, scenario_id)` with no `attributes`, which
   retires every version the scenario held.
4. Mark its `indicator_result` rows `status <> 'current'`, so the overlay tile
   stops serving a deleted proposal.
5. Delete the scenario row. Its name becomes reusable.

`jobs_runtime.job_run` rows stay as run history. `SCENARIOS_LAYER.md` currently
says scenario results "stay readable for comparison" because there is no
scenario row to archive; this makes that false and the sentence must change.

### The attribute allowlist, mechanically *(my call)*

`overridable_attributes()` derives seven attributes from what the indicators
declare they read: `geometry`, `floors`, `poi_version`, `network_version`,
`num_levels`, `unit_size`, `people_per_household`. Only four are authorable —
`geometry` is not a number, and `poi_version`/`network_version` are
hand-bumped invalidation counters.

Add `AUTHORABLE_ATTRIBUTES` to `indicators/catalog.py`, derived from
`overridable_attributes()` minus a named exclusion set, with each exclusion
carrying its reason. The contract test asserts it is a subset of the derived
set and that the exclusions are exactly the counters and geometry — so a new
indicator reading a new policy attribute appears automatically, and the list
cannot rot the way `poi_version` did for six migrations.

### CLI

```bash
python -m apps.cli.app scenario create --name "Centralidad norte" --aoi-id <uuid>
python -m apps.cli.app scenario list
python -m apps.cli.app scenario show --scenario-id <uuid>
python -m apps.cli.app scenario add-edit --scenario-id <uuid> \
    --geojson area.geojson --levels 6 --unit-size 60 --people-per-household 3.6
python -m apps.cli.app scenario materialize --scenario-id <uuid>
python -m apps.cli.app evaluate-scenario --scenario-id <uuid> --aoi-id <uuid>
```

`--levels` writes both `floors` and `num_levels`. `evaluate-scenario` already
exists and is unchanged.

### API

| | |
|---|---|
| `GET /scenarios` | list, project-scoped |
| `POST /scenarios` | create |
| `GET /scenarios/{id}` | one, with its edits |
| `DELETE /scenarios/{id}` | the retirement above |
| `POST /scenarios/{id}/edits` | add a rule set over one geometry |
| `DELETE /scenarios/{id}/edits/{edit_id}` | remove a rule |
| `POST /scenarios/{id}/materialize` | rebuild overrides from edits; returns counts |

A new `apps/api/routers/scenarios.py`.

⚠ **`GET /indicators` must grow `scenario_id`** *(my call, and it reverses a
documented decision)*. `list_indicators(with_values_for_aoi=...)` filters to
`scenario_id IS NULL` on the stated grounds that "a value that exists only
inside a what-if is not a reason to offer the indicator on the baseline map".
That is right for the baseline and fatal here: the five revived indicators
have rows *only* under the scenario, so without this the picker never offers
them and the whole feature's payoff is invisible. The probe becomes an
r043-style overlay — this scenario's rows, else the baseline's — and
`SCENARIOS_LAYER.md`'s "baseline-only by design" note must be amended.
*(The earlier claim that every indicator endpoint already takes `scenario_id`
was wrong; this one does not.)*

### Frontend

- `selection.ts` already emits a `Feature<Polygon>` for circle, rectangle and
  polygon. Its output becomes the edit geometry unchanged.
- A scenario picker beside the indicator picker, listing the active AOI's
  scenarios plus `(baseline)`.
- With a scenario active and an area drawn: one form with **levels, unit size
  and people per household**, saved as one act.
- "Recompute" calls materialize + run through the existing `POST /jobs`.
- **`renderIndicator` (`main.ts:524`) must pass `scenario_id` into the tile
  URL.** It does not today, although `choropleth.ts:5` and r043's docstring
  both say it does. That drift is pre-existing and this is where it is fixed.

---

## What this does not give you

- **A scenario does not survive `DROP DATABASE`.** `mexicali_run.sh` drops the
  database on every run without `--keep`, and that removes the scenario tables
  too. Export and import are **not** built here, by decision. Until they are,
  a proposal lives in one database and a pilot re-run destroys it.
- **No comparison view.** One scenario at a time against the baseline.
- **No capacity model beyond the existing formula.** `housing_units =
  (area × num_levels) / unit_size` stays a declared placeholder that uses block
  area for lot area; a scenario supplies better inputs, not a better formula.
- **No runtime tenant isolation.** The RLS policy matches the existing pattern
  and the existing pattern is not enforced.
- **Nothing about who proposed it.** No author column; the platform has no
  users yet.

## The risk I am not closing

**Materialisation cost at city scale is unmeasured.** A rule over a district
of 19,058 blocks produces up to one override row per block *per attribute* —
four attributes, so up to four rows each — and `publish_scenario_version`
digests every row for an attribute. The membership work measured an analogous
polygon-vs-feature pass at nine minutes before `ST_Subdivide` brought it to
forty seconds, so the shape of this risk is known and so is its remedy. The
builder should measure a full-district edit before assuming the interaction
can be synchronous. If it cannot, the remedy is the existing one: submit it
through the job layer rather than inventing a second path.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | edit-to-override resolution: later edits win; removed edits remove their rows; one `--levels` value produces both `floors` and `num_levels` |
| `tests/integration` | materialisation is idempotent; re-materialising publishes no version; a removed edit *retires* its attribute version; `ST_CoveredBy` excludes a straddling block; delete retires versions and supersedes results; RLS responds to a hand-set `app.current_project_id` |
| `tests/contract` | `AUTHORABLE_ATTRIBUTES` is a subset of `overridable_attributes()` and excludes exactly geometry and the version counters |
| `tests/e2e` | draw an area, set the three values, recompute, and see the choropleth change |

⚠ **The proof test needs its own fixture.** "The five indicators produce rows
under a scenario on the synthetic territory" passes *today*, before any of this
exists, because `synthetic_territory.py:116-119` seeds `floors`, `num_levels`,
`unit_size` and `people_per_household` on every block. The test must build its
own attribute-free blocks — `tests/integration/_helpers.insert_feature` already
does this — assert the five indicators write nothing, apply a scenario, and
assert they then write values. Changing the synthetic territory instead would
break the acceptance run's all-twelve-indicators claim and the contract test
that guards it. *(Caught by the builder review; the test as first specified
proved nothing.)*

## Maintenance checklist

1. Migration r048 — two tables, RLS policy, indexes
2. `serving_geo` unchanged
3. `explore` gains a scenario listing view
4. CLI listing commands read from `explore`
5. Tests per the table above
6. `AGENTS.md`; `SCENARIOS_LAYER.md` (the partial FK reversal, the
   baseline-only picker note, the "results stay readable" sentence);
   `TERMINOLOGY.md` ("a bare uuid, not a row" is no longer true for authoring)

## Left to the builder

- Exact request and response shapes for the endpoints.
- Whether `materialize` runs synchronously or through the job layer — decide
  it from the measurement the risk section calls for.
- The sidebar's layout and where the scenario controls sit.
- Whether `explore.scenario_v` exposes edit counts, override counts, or both.
