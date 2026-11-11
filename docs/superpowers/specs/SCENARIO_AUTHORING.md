# Design: scenarios — change an input, recompute what it touches, compare

A scenario is a hypothetical change to the environment. Anything the
indicator catalog declares as an input can change; the indicators that read
it recompute; and the result is readable against the baseline it departs
from.

**Status:** design, third framing. Written before the code.

---

## Intent

The contract is the design of an urban centrality. The platform describes the
city as it is; a scenario is how it describes a city that does not exist yet.

The question a scenario answers has a shape: *what would happen to these
numbers if this changed?* Close a street. Add a park. Allow six storeys in
this district. Each is a change to something the catalog already reads, and
each has a set of indicators it should move and a larger set it should leave
alone.

**The half you would expect to be hard is already built.** The asset graph
records which indicators read which input, and `evaluate-scenario` derives the
dirty set from a change and recomputes exactly those. What is missing is the
ability to *express* the change.

---

## What a scenario can change

The catalog declares ten inputs. Six are indicator-to-indicator edges the
graph propagates through on its own. Four are data:

| Input | Read by | A scenario changes it by |
|---|---|---|
| `mex.block` (self) | 10 of 12 | overriding `num_levels`, `unit_size`, `people_per_household`, `floors` |
| `global.road_network` | 4 | adding or removing streets |
| `global.poi` | 4 | adding or removing amenities |
| `global.building` | 1 | adding or removing footprints |

Today a scenario can do the first and only the first. This design adds the
other three through **one** mechanism, because roads, amenities and buildings
are all `curated_geo.feature` rows.

So there are exactly two mechanisms:

1. **Attribute override** on an existing feature — `scenario_override`
   exists, and `effective_numeric` already resolves
   `override ⊕ observation ⊕ base`.
2. **Feature-set override** — a scenario adds or removes a feature. New.

---

## Goals

- A scenario is **named, listed, found again**.
- A scenario can change **any declared input**, not a hardcoded list.
- A proposal is stored as **a rule**, so it survives a re-ingest.
- **Before and after are comparable** — per block and in summary.
- **Every failure says why.** A blank map after drawing must name its cause.
- One authoring surface, **not an editor per layer**.

## Non-goals, and these are what make it feasible

- **No GIS editor.** Add and remove only — no moving, reshaping or splitting
  an existing feature.
- **No edge splitting.** An added street connects two *existing* nodes. A
  street crossing another mid-span does not create a junction.
- **No added analysis units.** A scenario adds inputs, never blocks or AGEBs.
  See "Why added features are inputs only".
- **No per-layer semantics.** The attribute form is generated from what the
  catalog declares; a layer with nothing declared gets geometry alone.
- **No topology validation** beyond "the geometry is valid".
- **No scenario versioning, branching, or export/import.**
- **No runtime tenant isolation** — the RLS pattern is matched, not fixed.

## Anti-goals — failure even if it works

- **A scenario you cannot find again.**
- **Silent anything.** A write that reports success and persists nothing; an
  edit whose removal leaves its effect on the map; a scope that widens because
  something else was deleted; a change that recomputes and moves no number
  because it changed nothing real. Earlier drafts contained all four.
- **Silent defaults.** A value nobody chose.
- **Touching the baseline.**

---

## Constraints the builder must respect

Each verified against the code.

| | |
|---|---|
| **API writes need their own connection** | ⚠ `apps/api/deps.py:109-138` rolls back on the **success** path. `jobs.py:629-631`: *"the pooled one rolls back on teardown, which would make this look like it worked and change nothing."* No write here uses `Depends(get_db_conn)`. |
| **`indicator_result.feature_id` FKs to `curated_geo.feature`** | `ON DELETE CASCADE`. Anything receiving results must be a baseline feature. This is what bounds added features to inputs. |
| **`aoi_feature` has no FK on `feature_id`** | A plain column with an index, so scenario-owned features *could* be written there. They must not be: it is baseline membership. |
| **Supersession is scoped to rows written** | `repositories.py:2617-2652` returns early on empty input and marks stale only the features being written. A feature dropping out of a recompute keeps its old `current` row. |
| **The anti-join rule has a scope** | r043's anti-join is required for reads that **emit rows** — five call sites plus `indicator_overlay_tile`. Not required inside an `EXISTS`, where double-counting cannot occur. |
| **`valid_max` raises, it does not clamp** | `r016:303-312`. See "The ceiling on levels". |
| **`network_version` is a dirty marker, not a lever** | Written by `seed_demo_attributes.py` and bumped by hand; nothing in the ingest produces it. Overriding it recomputes accessibility *against the identical network* and returns identical numbers. It must not appear in the authoring surface. |
| **RLS is cosmetic here** | Nothing sets `app.current_project_id` outside tests; owners bypass RLS; `ENGINEERING_NOTES.md:112-120` lists what is missing. |
| **Bulk writes only** | `apply_overrides` publishes once per attribute; per-edit is quadratic. |
| **An AOI is the precondition for visibility** | `get_tile` scopes by `aoi_feature`; a feature in no AOI returns 204 everywhere. |
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

`project_id` derives from the AOI. RLS: `tenant_isolation` on `project_id`,
matching the other two tables and inheriting their limits.

### `curated_geo.scenario_edit` — the attribute rule

```sql
CREATE TABLE curated_geo.scenario_edit (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  geometry      geometry(Geometry, 4326) NOT NULL
                CHECK (ST_GeometryType(geometry) IN ('ST_Polygon','ST_MultiPolygon')),
  type_id       uuid NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
  attribute_key text NOT NULL,
  value_numeric double precision NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
```

`type_id` not a `'block'` string: a feature type is `(context, type_slug)`
everywhere, and the FK makes a typo impossible. The geometry CHECK matches
`area_of_interest` (`r004:23-24`) — a TerraDraw polygon can self-intersect,
and `ST_Covers` against an invalid one either raises or answers wrongly. No
`value_text`: every authorable attribute is numeric and `effective_numeric`
reads `value_numeric` only, so a text edit would be storable and unreadable.

### `curated_geo.scenario_feature` — the feature-set rule

```sql
CREATE TABLE curated_geo.scenario_feature (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id  uuid NOT NULL REFERENCES app_core.scenario(id) ON DELETE CASCADE,
  op           text NOT NULL CHECK (op IN ('add','remove')),
  type_id      uuid NOT NULL REFERENCES curated_geo.feature_type_catalog(id),
  -- op='add': the new feature's own geometry and attributes
  geometry     geometry(Geometry, 4326),
  name         text,
  attributes   jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- op='remove': the area whose features of this type are removed
  removal_area geometry(Geometry, 4326),
  created_at   timestamptz NOT NULL DEFAULT now(),
  CHECK ((op = 'add'    AND geometry IS NOT NULL AND removal_area IS NULL)
      OR (op = 'remove' AND removal_area IS NOT NULL AND geometry IS NULL))
);
```

**Removal is a rule, not a list of ids**, for the same reason the attribute
edit is: it re-applies after a re-ingest, where feature ids are regenerated
but the drawn area still means the same thing.

#### Why added features are inputs only

An added feature never receives an `indicator_result` row, so it never needs
to satisfy that table's FK to `curated_geo.feature`. That is the whole reason
this can be a separate table.

It also means **a scenario cannot add a block.** Adding an analysis unit would
require a real `curated_geo.feature` row, AOI membership, and a tile path —
three things that would each have to be made scenario-aware. A scenario adds
the things blocks are *measured against*: amenities, streets, buildings.

#### Why a separate table rather than a `scenario_id` column on `feature`

Both work. The separate table **fails safe**.

With a nullable `scenario_id` on `curated_geo.feature`, every baseline read in
the codebase must remember to filter it out, and one that forgets silently
leaks a hypothetical feature into the real city — the exact failure r043's
anti-join exists to prevent, reintroduced at a lower layer. With a separate
table, a read that forgets the union simply does not see the addition: the
scenario under-reports, which is visible and harmless.

### How a read becomes scenario-aware

```
baseline features  −  removed by this scenario  +  added by this scenario
```

Concretely, at each of the ~8 indicator read sites:

```sql
-- was: FROM curated_geo.feature f WHERE f.type_slug = 'establishment'
WITH scoped AS (
    SELECT f.id, f.geometry, f.attributes
    FROM curated_geo.feature f
    WHERE f.type_slug = 'establishment'
      AND NOT EXISTS (
            SELECT 1 FROM curated_geo.scenario_feature sf
            WHERE sf.scenario_id = %(scenario_id)s AND sf.op = 'remove'
              AND sf.type_id = f.type_id
              AND ST_Covers(sf.removal_area, f.geometry_centroid))
    UNION ALL
    SELECT sf.id, sf.geometry, sf.attributes
    FROM curated_geo.scenario_feature sf
    WHERE sf.scenario_id = %(scenario_id)s AND sf.op = 'add'
      AND sf.type_id = %(type_id)s
)
```

With `scenario_id IS NULL` the two extra clauses collapse to nothing and the
baseline query is unchanged — which is what keeps one code path for both.

### What a drawn area catches

**`ST_Covers(area, f.geometry_centroid)`** — the rule the platform already
uses. `count_features_within_polygon` (`repositories.py:1775-1800`) answers
this question for the selection panel on the centroid, with `ST_Covers` rather
than `ST_Contains` so a centroid on a shared edge is not silently dropped, and
`selection-filter.ts` mirrors it client-side *specifically so the two cannot
disagree*.

⚠ An earlier draft specified `ST_CoveredBy` on full geometry and cited
`compute_overlaps` as precedent. That citation was wrong — there
`ST_CoveredBy` is a cost short-circuit and the membership gate is
`ST_Intersects`. Using it would have made the sidebar report "N blocks
selected" while the scenario edited a smaller set.

### Roads, the one layer needing more

The accessibility indicators route over `curated_geo.road_edge`, not over
`global:road` features, so a road change must reach the graph.

`_EDGES_SQL` (`accessibility.py:65`) becomes a function of scenario:

```sql
SELECT id, source, target, cost, reverse_cost FROM curated_geo.road_edge
WHERE NOT EXISTS (… scenario removals covering this edge …)
UNION ALL
SELECT … FROM curated_geo.scenario_road_edge WHERE scenario_id = %s
```

`scenario_road_edge` is **materialised, not authored** — derived from the
`scenario_feature` rows of road type. An added street's endpoints snap to the
nearest existing `road_node` with `ORDER BY rn.geometry <-> p LIMIT 1`, the
pattern `accessibility.py:168-180` already uses for amenities and block
centroids, and `cost` is its geodesic length.

⚠ **An added street connects existing nodes and nothing else.** It does not
split an edge it crosses. Two added streets that cross do not meet. This is
the single largest simplification in the design and the one most likely to
surprise someone, so the UI says it where the street is drawn.

### Comparison

A first-class goal, and cheap: baseline and scenario results already coexist
in `indicator_result` — that is r043's entire design — and
`acceptance_run.sh:242-269` already computes a per-feature delta with a LEFT
JOIN and reports "blocks the scenario changed: N of M".

- `GET /scenarios/{id}/comparison?indicator_slug=…` returns per-feature
  `{baseline, scenario, delta}` plus a summary: features changed, mean delta,
  and the direction that counts as better, which the catalog already declares
  as `higher_is_better`.
- The map gains a **delta mode**: a diverging ramp over `scenario − baseline`
  instead of the value. ⚠ Diverging, centred on zero, and a different palette
  from the sequential value ramp — a delta rendered in the value ramp reads as
  a value.
- Blocks the scenario did not touch show as "unchanged", not as absent.

### The ceiling on levels

`edificability_block_density` reduces to `10000 × num_levels / unit_size` —
block area cancels, as the catalog's own comment says — with `valid_max=2000`
enforced by a trigger that **raises**. So **`levels ≤ unit_size / 5`**, and a
violation fails for every block at once and takes the livability composite
with it. The authoring surface validates against the bound read from the
catalog and refuses before writing, naming both numbers.

### Materialisation

`materialize_scenario(conn, *, scenario_id)`:

1. **Refuse if the scenario has no AOI.** `aoi_id` is `ON DELETE SET NULL`, so
   a deleted AOI would otherwise silently convert a scoped rule into one over
   every feature in the database.
2. Attribute edits → features by `type_id` whose centroid the area covers,
   scoped to the AOI; later edits win.
3. Compute which features **lose** an override, delete those rows, keep the ids.
4. Apply the rest in **one** `apply_overrides` call.
5. Rebuild `scenario_road_edge` from the road-type `scenario_feature` rows.
6. `publish_scenario_version(conn, scenario_id)` with **no `attributes`
   argument** — reconciles retirements, which a targeted publish does not.
7. ⚠ **Supersede the results the removed overrides produced.** Mark this
   scenario's `current` rows `'stale'` for the step-3 ids and the indicators
   reading the removed attributes.

Step 7 is not optional: supersession in `bulk_write_numeric_results` is scoped
to the features being written and returns early on empty input, so a block
that loses its override is skipped by the compute, never written, and keeps
its old `current` row for the tile to paint. Without it, removing every edit
leaves the choropleth untouched.

`'stale'` rather than a new status value: the CHECK allows only `current`,
`stale`, `failed`, and `stale` is already the platform's supersession marker.

Materialisation is **idempotent** and **reports per rule how many features it
matched**. Zero is returned and surfaced, not logged.

### The asset graph

Overrides are registered as a `kind="source"` leaf whose version is its state
(`registry.py:224-239`). Making them derived without telling the graph would
leave "edited but not materialised" reporting fresh — the silent staleness
this document exists against.

r048 adds `'scenario_edit'` and `'scenario_feature'` to the `backing_kind`
CHECK (`r041:132-137`, a closed list) and registers both as sources upstream
of the overrides and of the layers they modify. The layer edges matter: an
added park must make `global.poi` stale *for that scenario* so the four
accessibility indicators replan.

### Deleting a scenario

One transaction: delete overrides → `publish_scenario_version` with no
attributes → mark results `'stale'` → delete `asset_materialization` rows →
delete the scenario row, cascading edits, features and road edges.

The fourth step is not cosmetic: `asset_component_v` (`r041:405-417`) filters
materialisation rows on nothing, so a deleted scenario's partition stays a
live node reporting stale forever and `assets stale` accumulates one entry per
deleted scenario naming something that no longer exists.

### The authoring surface — one form, not an editor

| Step | Control |
|---|---|
| Where | the existing draw tools (`selection.ts`) |
| What layer | the feature types the catalog declares as inputs |
| What change | *set an attribute* · *add one here* · *remove those here* |
| Values | fields **generated from the catalog** — `AUTHORABLE_ATTRIBUTES` for the chosen type |

Nothing is layer-specific. A new indicator declaring a new input gives its
layer a form for free. `network_version` and `poi_version` never appear — they
are dirty markers, and overriding one recomputes the identical answer.

**Added features are drawn, so the client already has their geometry.** They
render as a local layer, needing no scenario tile path and no `aoi_feature`
rows. This is what keeps added features out of the baseline's membership and
tile machinery entirely.

### Transactions

Every write opens its own connection and commits, following `jobs.py`. A
contract test asserts no handler in `routers/scenarios.py` depends on the
pooled connection — the failure it prevents is a 200 with nothing written.

### CLI

```bash
scenario create --name "Centralidad norte" --aoi-id <uuid>
scenario list / show --scenario-id <uuid>
scenario set-attribute --scenario-id <uuid> --geojson area.geojson \
    --type MEX:block --levels 6 --unit-size 60 --people-per-household 3.6
scenario add-feature --scenario-id <uuid> --type global:establishment \
    --geojson park.geojson --attribute category=park
scenario remove-features --scenario-id <uuid> --type global:road \
    --geojson area.geojson
scenario materialize --scenario-id <uuid>
evaluate-scenario --scenario-id <uuid> --aoi-id <uuid>
scenario compare --scenario-id <uuid> --slug accessibility_block_park_network_m
```

`--levels` writes both `floors` and `num_levels`: the same quantity under two
names (`seed_demo_attributes.py:23-40`), and two fields would leave
`planned_floors` on the baseline while edificability moved.

### API

`GET`/`POST /scenarios`, `GET`/`DELETE /scenarios/{id}`,
`POST`/`DELETE /scenarios/{id}/edits`, `.../features`,
`POST /scenarios/{id}/materialize`, `GET /scenarios/{id}/comparison`.

**`GET /indicators` grows `scenario_id`**, because the five revived indicators
have rows only under a scenario and the picker would never offer them. ⚠ Use
`scenario_id = X OR scenario_id IS NULL` inside the probe: it is an `EXISTS`,
emits no rows, so the anti-join is unnecessary and would buy a correlated
`NOT EXISTS` per catalog row for nothing. `listIndicators` (`api.ts:104`) takes
only `aoiId` and must carry it too. `activateIndicator`
(`choropleth.ts:163-181`) must pass `scenario_id` into the tile URL beside
`aoi_id` at `:180`.

---

## The demo this is built to support

Each is one drawn shape and one form:

| Change | Moves | Leaves alone |
|---|---|---|
| Add a park | the four accessibility indicators | urban form, edificability |
| Close a street | accessibility, through the routing graph | everything else |
| Connect two neighbourhoods | accessibility | everything else |
| Allow six storeys here | the five currently-empty indicators | accessibility |

The last row is the reason this started: those five write nothing on real
Mexicali data because `num_levels`, `unit_size` and `people_per_household` are
policy choices no survey carries — Overture has `num_floors` on 0.30% of
Mexicali's 387,273 buildings, so deriving them would invent 99.7% of the
answer.

The first row is the one worth showing, because it closes a loop: park
accessibility was a flat 1,500 m everywhere until destinations were ingested,
and a scenario that adds one more park makes it move again.

---

## What this does not give you

- **No survival of `DROP DATABASE`** — `mexicali_run.sh` drops the database on
  every run without `--keep`, taking the scenario tables with it. The rule
  re-applies after a *re-ingest*, which is a smaller claim than an earlier
  draft made.
- **No added blocks**, no reshaping, no edge splitting, no crossing junctions.
- **Values only where the proposal reaches**, for the five revived indicators:
  outside the drawn area a block has no override, so there is no scenario row
  and no baseline row to fall back to. Correct, but the UI must say so rather
  than let a painted patch in a blank AOI read as a rendering fault.
- **No runtime tenant isolation**, **no author**, **no comparison between two
  scenarios** — only against the baseline.

## The risk I am not closing

**Cost at city scale is unmeasured, now in two places.** A rule over 19,058
blocks writes up to one override row per block per attribute, and
`publish_scenario_version` digests every row for an attribute; and the
scenario-aware edge query adds a `NOT EXISTS` and a `UNION ALL` inside the
graph every `pgr_drivingDistance` walks. The membership work measured an
analogous polygon pass at nine minutes before `ST_Subdivide` brought it to
forty seconds, so the shape and the remedy are known. Measure a full-district
edit and a scenario accessibility run before assuming either can be
synchronous; if not, use the job layer rather than a second path.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | later edits win; `--levels` writes both keys; the density ceiling is derived from the catalog; an added street's endpoints snap to existing nodes |
| `tests/integration` | the overlay resolves `baseline − removed + added`; with `scenario_id IS NULL` the query is byte-identical to the baseline one; a removed edit retires its version **and** supersedes its results; `ST_Covers` on centroid matches `count_features_within_polygon`; materialise refuses without an AOI; delete removes overrides, versions, results and materialisation rows; an invalid polygon is repaired |
| `tests/contract` | `AUTHORABLE_ATTRIBUTES` is the derived set minus geometry and the two version markers; no `scenarios.py` handler depends on `get_db_conn`; no scenario-owned feature is ever written to `curated_geo.feature` or `aoi_feature` |
| `tests/e2e` | add a park, recompute, and see park accessibility improve in delta mode |

⚠ **The proof test needs its own fixture.** "The five indicators produce rows
under a scenario on the synthetic territory" passes *today*, because
`synthetic_territory.py:116-119` seeds all four attributes on every block. The
test must build attribute-free blocks with `_helpers.insert_feature`, assert
the five write nothing, apply a scenario, and assert they then write values.

⚠ **The accessibility proof must assert a number moved.** A scenario that
recomputes and returns identical values is the `network_version` trap. The
test adds a park inside a block's catchment and asserts that block's distance
*decreased*.

## Maintenance checklist

1. Migration r048 — three tables, RLS, indexes, the `backing_kind` CHECK
2. `serving_geo` unchanged
3. `explore` gains a scenario view
4. CLI listing reads from `explore`
5. Tests per the table above
6. Docs: `AGENTS.md`; `SCENARIOS_LAYER.md` (the FK reversal, the baseline-only
   picker, "results stay readable"); `TERMINOLOGY.md` ("every `scenario_id`
   column is a plain uuid" is no longer true); `ENGINEERING_NOTES.md:108-110`
   ("a scenario→project association that does not exist" — it now does);
   **`docs/delivery/03_database_and_api_reference.md`**, generated, hashed and
   guarded by `test_repository_structure.py:427-478` — regenerate it and the
   manifest
7. **`acceptance_run.sh:185-223`** mints a scenario from `uuid5` with no
   `app_core.scenario` row, which would make `explore.scenario_v` report zero
   scenarios right after the Schedule A run creates one. Move it onto the new
   CLI.

## Left to the builder

- Request and response shapes.
- Whether `materialize` and the scenario accessibility run are synchronous or
  go through the job layer — decide from the measurement above.
- Sidebar layout and the delta ramp's palette.
- Whether `explore.scenario_v` exposes edit counts, feature counts, or both.

---

## What the reviews changed

The first draft read as finished and was not. A builder review found eight
gaps; an adversarial review found fifteen more, four verified against the code
before any was accepted. Then the framing itself turned out to be wrong.

**Framing.** Drafts one and two designed *block attribute authoring*. The
actual concept is *override any declared input* — which is a different schema,
because the editable set is derived from the asset graph rather than
hardcoded, and because adding and removing features is half of it. Comparison
was a declared non-goal in both drafts and is half the point. Both errors came
from inferring the purpose instead of asking for a concrete case.

**Would have shipped silent failures.** Every write persisting nothing;
removing an edit leaving its result on the map; deleting an AOI widening a
rule to the whole database; forgetting to materialise reporting everything
fresh.

**Would have failed at runtime.** Levels above `unit_size / 5` raise a plpgsql
exception that aborts every block at once.

**Was factually wrong.** The `ST_CoveredBy` justification cited a precedent
that says the opposite; "every indicator endpoint already takes `scenario_id`";
`renderIndicator` as the tile-URL site; "six readers" (five, plus the tile); an
RLS test that cannot pass because the test role owns the tables; a proof test
that passed before the feature existed.
