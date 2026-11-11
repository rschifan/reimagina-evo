# Design: scenario authoring, so a proposal can be made and measured

Give a scenario an identity and a way in. Today a scenario is a bare uuid
that can only be created by running Python, and the proposal it represents
exists nowhere — only the per-block rows it happened to write.

**Status:** design. Written before the code.

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
- A scenario **survives a data reload** and can be re-applied to better data.
- Someone can **draw an area on the map, set a value, and see the indicators
  recompute** against the proposal.
- The five currently-empty indicators produce real values under a scenario.

## Non-goals

- **Scenario comparison.** Viewing two proposals side by side is a later
  piece; this one has to exist first.
- **Versioning or branching** of a scenario's history.
- **A zoning model.** No land-use classes, no setbacks, no FAR tables. A
  scenario sets the attributes the indicators already read, and no others.
- **Permissions beyond what exists.** The project-scoped RLS pattern is
  followed, nothing new is invented.
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
| **RLS pattern** | `app_core.project` and `curated_geo.area_of_interest` carry a `tenant_isolation` policy on `app.current_project_id`. A project-scoped scenario table must carry the same, or scenarios leak across projects. These are the only two RLS tables; match them exactly. |
| **Bulk writes only** | `apply_overrides` publishes once per attribute. Per-edit publishing is quadratic — the docstring measures upzoning 10,000 blocks at ~50 million row reads. Materialisation must call `apply_overrides`, never `apply_override` in a loop. |
| **Per-attribute versions** | `publish_scenario_version` versions `scenario.override.<attr>@scenario=S` separately (r045). This is what makes selective recompute exact: editing `floors` must not invalidate indicators that read `num_levels`. Do not collapse to one scenario-wide version. |
| **The read path is finished** | r043 resolves scenario-over-baseline as an **anti-join** across six readers and the overlay tile. ⚠ Never widen it to `scenario_id = X OR scenario_id IS NULL` — that double-counts every recomputed feature. Nothing in this design should need to touch it. |
| **The pilot drops its database** | `mexicali_run.sh` runs `DROP DATABASE IF EXISTS` on every run without `--keep`. A scenario that exists only as override rows is destroyed by a routine re-run. This is the main argument for storing the rule. |
| **Migration head is r047** | The new migration is r048. |

---

## Approaches considered

**A — Identity only.** Add `app_core.scenario` for the name and project link.
Drawing an area writes one override row per block inside it; the scenario *is*
its list of edits.
*Trade-off:* the smallest migration and closest to what exists. But a pilot
re-run deletes every scenario, and nothing records what rule produced four
thousand override rows — the edits are visible, the intent never is.

**B — The scenario is a rule.** `app_core.scenario` plus
`curated_geo.scenario_edit`: an area, an attribute, a value. Override rows
stop being authored and become *derived* — materialising the edits produces
them.
*Trade-off:* two tables and a materialisation step rather than one table. In
exchange the proposal survives a reload, re-applies to better data, and maps
one-to-one onto the UI: draw, pick attribute, set value, save.

**C — No new tables.** An endpoint takes polygon + attribute + value, writes
overrides, returns the uuid. Honours the recorded deferral exactly.
*Trade-off:* fastest and argues with nothing, but scenarios stay unnameable
and unfindable — the current situation with a nicer front door.

**Chosen: B**, by Rossano. The deferral was recorded when a scenario was a
test fixture. There is now a real pilot and a contract about producing a
design, and a design that cannot outlive a database reload is not one.

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
destroy it. `UNIQUE (project_id, name)` is what makes "find it again" work —
a scenario named twice in one project is a mistake, not two scenarios.

**RLS:** enable row security and add `tenant_isolation` with the same
predicate shape the other two tables use, keyed on `project_id`.

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

The `CHECK` mirrors `scenario_override`'s, so the two cannot disagree about
what a value is.

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

It must be **idempotent**: materialising twice changes nothing and, because
`publish_scenario_version` digests content, publishes nothing — so a scenario
is not recomputed for being saved twice.

⚠ **`ST_CoveredBy`, not `ST_Intersects`.** A block straddling the boundary of
a drawn area is not "in" the proposal, and counting it makes the rule's effect
depend on where the user happened to release the mouse. `compute_overlaps`
already uses the covered-by/intersection split for the same reason; follow it.

### The FK question — how far the deferral is reversed

`scenario_edit.scenario_id` gets a real foreign key: both sides are new, so
there is nothing to backfill.

`scenario_override.scenario_id`, `indicator_result.scenario_id` and
`jobs_runtime.job_run.scenario_id` **keep their bare uuids**. Those columns
hold historical ids with no scenario row — the acceptance run mints one from
`uuid5` — and backfilling placeholder scenarios to satisfy a constraint would
invent records of proposals nobody made. The deferral is therefore reversed
for *authoring* and left in place for *results*, which is a smaller claim than
r016 anticipated and should be recorded as such in `SCENARIOS_LAYER.md`.

### CLI

```bash
python -m apps.cli.app scenario create --name "Centralidad norte" --aoi-id <uuid>
python -m apps.cli.app scenario list
python -m apps.cli.app scenario show --scenario-id <uuid>
python -m apps.cli.app scenario add-edit --scenario-id <uuid> \
    --geojson path/to/area.geojson --attribute num_levels --value 6
python -m apps.cli.app scenario materialize --scenario-id <uuid>
python -m apps.cli.app evaluate-scenario --scenario-id <uuid> --aoi-id <uuid>
```

`evaluate-scenario` already exists and is unchanged — it derives its dirty set
from the overrides materialisation just wrote.

### API

| | |
|---|---|
| `GET /scenarios` | list, project-scoped |
| `POST /scenarios` | create |
| `GET /scenarios/{id}` | one, with its edits |
| `DELETE /scenarios/{id}` | cascade to edits and overrides |
| `POST /scenarios/{id}/edits` | add a rule; body carries GeoJSON geometry, attribute, value |
| `DELETE /scenarios/{id}/edits/{edit_id}` | remove a rule |
| `POST /scenarios/{id}/materialize` | rebuild overrides from edits; returns counts |

A new `apps/api/routers/scenarios.py`. The read side needs nothing: every
indicator endpoint already takes `scenario_id`.

### Frontend

The pieces exist and are not connected.

- `selection.ts` already emits a `Feature<Polygon>` for circle, rectangle and
  freehand polygon, and already has subscribers. Its output becomes the edit
  geometry unchanged.
- A scenario picker beside the indicator picker: `(baseline)` plus the
  project's scenarios.
- With a scenario active and an area drawn: choose an attribute, type a value,
  save. Then "Recompute", which calls materialize + run and reports progress
  through the existing ops plane.
- **`showIndicator` must pass `scenario_id` into the tile URL.** It does not
  today, although `choropleth.ts:5` and `r043`'s docstring both say it does.
  That drift is pre-existing and this is where it gets fixed.

The attribute list is **closed**: the attributes the catalog declares
(`num_levels`, `unit_size`, `people_per_household`, `floors`). A free-text
attribute key would let someone author an override no indicator reads, which
looks like it worked and changes nothing.

---

## What this does not give you

- **No comparison view.** One scenario at a time against the baseline.
- **No capacity model beyond the existing formula.** `housing_units =
  (area × num_levels) / unit_size` stays a declared placeholder that uses block
  area for lot area; a scenario supplies better inputs, not a better formula.
- **No answer for AOI-less scenarios.** With `aoi_id` NULL, materialisation
  falls back to the edit geometry alone, which on a national dataset would be
  slow. Acceptable because every real scenario here has an AOI.
- **Nothing about who proposed it.** No author column; the platform has no
  users yet.

## The risk I am not closing

**Materialisation cost at city scale is unmeasured.** A rule over a district
of 19,058 blocks produces up to one override row per block, and
`publish_scenario_version` digests every row for that attribute. The
membership work measured an analogous polygon-vs-feature pass at nine minutes
before `ST_Subdivide` brought it to forty seconds, so the shape of this risk
is known and so is its remedy. The honest position is that it is not measured
*here*, and the builder should measure a full-district edit before assuming
the interaction can be synchronous. If it cannot, the remedy is the existing
one: submit it through the job layer rather than inventing a second path.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | edit-to-override resolution: later edits win, removed edits remove their rows, a non-numeric value lands in `value_text` |
| `tests/integration` | materialisation is idempotent; re-materialising publishes no new version; `ST_CoveredBy` excludes a straddling block; deleting a scenario cascades to edits and overrides; RLS hides another project's scenarios |
| `tests/contract` | the attribute allowlist matches the catalog's declared attributes, so a scenario cannot author an override no indicator reads |
| `tests/e2e` | draw an area, set a value, recompute, and see the choropleth change — the one path that proves the pieces are connected |

A test that the five dead indicators produce rows under a scenario on the
synthetic territory belongs in `tests/integration`, and is the closest thing
to a proof that this work achieved its purpose.

## Maintenance checklist

1. Migration r048 — two tables, RLS policy, indexes
2. `serving_geo` unchanged — scenarios are read through existing surfaces
3. `explore` gains a scenario listing view, since a scenario is now a thing an
   engineer inspects
4. CLI commands read from `explore` where they list
5. Tests per the table above
6. `AGENTS.md`, `SCENARIOS_LAYER.md` (the partial reversal), `TERMINOLOGY.md`
   ("a bare uuid, not a row" is no longer true for authoring)

## Left to the builder

- Exact request and response shapes for the endpoints.
- Whether `materialize` runs synchronously or through the job layer — decide
  it from the measurement the risk section calls for.
- The sidebar's layout and where the scenario controls sit.
- Whether `explore.scenario_v` exposes edit counts, override counts, or both.
