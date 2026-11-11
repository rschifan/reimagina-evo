# Design: scenario authoring, so a proposal can be made and measured

Give a scenario an identity and a way in. Today a scenario is a bare uuid
that can only be created by running Python, and the proposal it represents
exists nowhere — only the per-block rows it happened to write.

**Status:** design, reviewed twice. Written before the code.

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

A scenario is where that choice legitimately lives.

---

## Goals

- A scenario can be **named, listed and found again** tomorrow.
- A proposal is stored as **a rule over an area** — not as the four thousand
  per-block rows it produces.
- A proposal **re-applies after a re-ingest**: feature ids change, the
  geometric rule does not.
- Someone can **draw an area, set the values, and see the indicators
  recompute** against the proposal.
- The five currently-empty indicators produce real values under a scenario.
- **Every failure says why.** A blank map after drawing an area must name its
  cause, not look like the bug this feature exists to fix.

## Non-goals

- **Scenario comparison**, **versioning/branching**, **a zoning model**,
  **export/import**, **changing what the indicators compute**.
- **Permissions beyond what exists.** This work does not build the deferred
  project-context enforcement; see the RLS constraint.

## Anti-goals — what would make this a failure even if it worked

- **A scenario you cannot find again.**
- **Silent defaults.** A value nobody chose, applied because it was handy,
  manufactures a finding — the failure the park indicator already shipped.
- **Silent anything.** A write that reports success and persists nothing, an
  edit whose removal leaves its effect on the map, a scope that widens because
  something else was deleted. Three of these were in the first draft.
- **Losing the rule.**
- **Touching the baseline.**

---

## Constraints the builder must respect

| | |
|---|---|
| **API writes need their own connection** | ⚠ `apps/api/deps.py:109-138` — `get_db_conn()` calls `connection.rollback()` on the **success** path, not only on error. Every write in this design must use its own committed connection, the pattern `jobs.py:629-631` documents: *"the pooled one rolls back on teardown, which would make this look like it worked and change nothing."* |
| **The deferral is recorded, and r016 promised the reverse** | `r016:7-10` says `app_core.scenario` "lands with Layer 3 and the FK is added together with the scenario table", and that `indicator_result` gets a CASCADE then. This design adds the table and **does not** add those FKs; r016's forward promise therefore becomes partly unmet and must be recorded, not left standing. |
| **RLS is cosmetic here** | Nothing in the application sets `app.current_project_id` — only tests. `r011` is permissive when unset, owners bypass RLS, and `ENGINEERING_NOTES.md:112-120` lists what is missing. The new table matches the pattern and inherits its limits. |
| **Bulk writes only** | `apply_overrides` publishes once per attribute; per-edit publishing is quadratic (~50M row reads for 10,000 blocks). |
| **Per-attribute versions** | `publish_scenario_version` versions `scenario.override.<attr>@scenario=S` separately (r045), which is what makes selective recompute exact. |
| **The anti-join rule has a scope** | r043's anti-join is required for reads that **emit rows** — five call sites (`repositories.py:2102, 2677, 2699, 2733, 2782`) plus `indicator_overlay_tile`. ⚠ It is *not* required inside an `EXISTS` probe, where double-counting is structurally impossible. See `GET /indicators`. |
| **`valid_max` is enforced by a RAISE** | `r016:303-312` raises when a result exceeds the catalog bound. It does not clamp. See "The ceiling on levels". |
| **Supersession is scoped to rows written** | `repositories.py:2617-2652` — `bulk_write_numeric_results` returns early on empty input and only marks stale the features it is writing. A feature that drops out of a recompute keeps its old `current` row. |
| **Migration head is r047** | The new migration is r048. |

---

## Approaches considered

**A — Identity only.** One table; the scenario *is* its override rows. Nothing
records the rule, and a re-ingest orphans every row.

**B — The scenario is a rule.** Two tables; overrides become derived.

**C — No new tables.** An endpoint that writes overrides and returns a uuid.
Scenarios stay unnameable.

**Chosen: B**, by Rossano.

⚠ **A correction to the argument originally made for it.** B was first
justified as surviving a data reload. That was wrong: `mexicali_run.sh` runs
`DROP DATABASE`, which takes the new tables too. The claim is narrowed to what
is true — **a rule re-applies after a re-ingest**, because it selects by
geometry rather than feature id, and because it says what was intended.

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

`project_id` derives from the AOI (`area_of_interest.project_id` is NOT NULL),
required explicitly otherwise. RLS: `tenant_isolation` on `project_id`,
matching the other two tables.

### `curated_geo.scenario_edit`

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
CREATE INDEX scenario_edit_scenario_idx ON curated_geo.scenario_edit (scenario_id);
CREATE INDEX scenario_edit_geometry_gist ON curated_geo.scenario_edit USING gist (geometry);
```

Three corrections from the first draft, each from a verified defect:

- **`type_id`, not a `feature_type` string.** A feature type is
  `(context, type_slug)` everywhere — `get_type_id(conn, "MEX", "block")` — so
  storing `'block'` alone drops the dimension that separates `MEX:block` from
  `global:road`. An FK also makes a typo impossible.
- **The geometry CHECK**, matching `area_of_interest` (`r004:23-24`). A
  TerraDraw polygon can self-intersect; an invalid polygon makes `ST_Covers`
  either raise a GEOS exception that aborts the whole materialise or answer
  wrongly. Writes repair with `ST_MakeValid` + `ST_CollectionExtract(…, 3)`,
  the pattern `test_aoi_geometry_repair_is_consistent.py` exists to enforce.
- **No `value_text`.** All four authorable attributes are numeric and
  `effective_numeric` reads `value_numeric` exclusively, so a text edit would
  be storable and silently unreadable. A text policy attribute is a schema
  change when one exists.

`attribute_key` gets no DB constraint: the authorable set is derived in Python
from what the indicators declare. It is validated at the write path, and the
contract test below is what keeps that honest.

### Which blocks a drawn area catches

**`ST_Covers(edit.geometry, f.geometry_centroid)`** — the rule the platform
already uses, not a new one.

`count_features_within_polygon` (`repositories.py:1775-1800`) answers exactly
this question for the selection panel, on the centroid, with `ST_Covers` and
not `ST_Contains` so a centroid on a shared block edge is not silently
dropped; and `selection-filter.ts` mirrors it client-side *specifically so the
two cannot disagree*. Its own comment calls it "the same one humans intuit as
'did the circle catch this?'".

⚠ The first draft specified `ST_CoveredBy` on full geometry and justified it
by citing `compute_overlaps`. That citation was wrong — there `ST_CoveredBy`
is a cost short-circuit inside a `sum(CASE …)`, and the membership gate is
`ST_Intersects`, so straddlers are *included*. Using it here would have made
the sidebar report "N blocks selected" while the scenario edited a strictly
smaller set, with nothing showing which.

### The ceiling on levels

`edificability_block_density` reduces to `10000 × num_levels / unit_size` —
block area cancels, as the catalog's own comment says — and its
`valid_max=2000` is enforced by a trigger that **raises**. So
**`levels ≤ unit_size / 5`** is a hard ceiling. Six levels at 60 m² is fine;
thirteen is not, and seven at 30 m² studios is not. Because area cancels, a
violation fails for every block at once, aborts the unit, and takes
`livability_block_score` with it.

The authoring surface **validates against the bound read from the catalog**,
not a hardcoded number, and refuses before writing with a message naming both
values. A unit test pins that the bound is derived.

### Materialisation

`materialize_scenario(conn, *, scenario_id)`:

1. **Refuse if the scenario has no AOI.** `aoi_id` is `ON DELETE SET NULL`, so
   deleting an AOI would otherwise silently convert an AOI-scoped rule into an
   unscoped one over every block in the database — a scope change with no edit
   and no notice, which the anti-goals forbid.
2. For each edit, select features by `type_id` whose `geometry_centroid` is
   covered by the edit geometry, scoped to the AOI through `aoi_feature`.
3. Collect `(feature_id, attribute_key, value)`; **later edits win**.
4. Compute the set of features that **lose** an override relative to the
   current rows, delete those rows, and remember the ids.
5. Apply the rest in **one** `apply_overrides` call.
6. `publish_scenario_version(conn, scenario_id)` with **no `attributes`
   argument**, which reconciles retirements — an attribute whose last override
   row went away in step 4 otherwise keeps a current version forever and its
   indicator never goes stale.
7. ⚠ **Supersede the results the removed overrides produced.** For the ids
   from step 4, mark this scenario's `status='current'` `indicator_result`
   rows `'stale'` for the indicators that read the removed attributes.

Step 7 is not optional and was missing from the first draft. Supersession in
`bulk_write_numeric_results` is scoped to the features *being written* and
returns early on empty input, so a block that loses its override is skipped by
`edificability.py:104`, never appears in the written rows, and keeps its old
`current` row — which `indicator_overlay_tile` goes on painting. Remove every
edit and the choropleth would survive untouched. Step 4 removed the invisible
half of that ghost; step 7 removes the half anyone can see.

`'stale'` is used rather than a new status value: the CHECK allows only
`current`, `stale`, `failed`, and `stale` is already the platform's marker for
"a newer write replaced this".

Materialisation is **idempotent**, and **reports per edit how many features it
matched**. Zero is returned to the caller and surfaced in the UI rather than
logged, because a tight circle that contains no block whole is a plausible
outcome whose only other symptom is a blank map.

### The asset graph

`scenario.override.<attr>` is registered as a `kind="source"` leaf whose
recorded version *is* its state (`registry.py:224-239`). This design makes
overrides derived, so without a change, editing a rule and forgetting to
materialise leaves the graph reporting every downstream indicator fresh —
reintroducing exactly the silent staleness this document exists against.

r048 therefore adds `'scenario_edit'` to the `backing_kind` CHECK
(`r041:132-137`, a closed list) and registers the edits as the source upstream
of the overrides. *(The first draft planned neither, and described the
migration as "two tables, RLS policy, indexes".)*

### Deleting a scenario

One transaction, in this order:

1. Delete this scenario's override rows.
2. `publish_scenario_version(conn, scenario_id)` with no `attributes` — retires
   every version it held.
3. Mark its `indicator_result` rows `'stale'`.
4. Delete its `asset_materialization` rows. Without this the deleted
   scenario's partition stays a live node in `asset_component_v`
   (`r041:405-417` filters on nothing), reports stale forever, and
   `assets stale` accumulates one entry per deleted scenario naming something
   that no longer exists.
5. Delete the scenario row; the edits cascade with it, and the name becomes
   reusable.

*(The first draft listed the edit deletion first and attributed it to a
cascade that fires last, and never mentioned `asset_materialization`.)*

`jobs_runtime.job_run` rows stay as run history.

### The attribute allowlist

`AUTHORABLE_ATTRIBUTES` lives **beside `overridable_attributes()` in
`assets/registry.py`**, derived from it minus a named exclusion set —
`geometry` (not a number) and `poi_version` / `network_version` (hand-bumped
counters). Not in `indicators/catalog.py`: `registry.py` defers its
`SHIPPED_INDICATORS` import to function scope in three places to keep that
dependency one-way, and putting the constant on the other side inverts it.

The contract test asserts it is a subset of the derived set and that the
exclusions are exactly those three.

### Transactions, for every write

Each write endpoint and CLI command opens its **own connection and commits**,
following `jobs.py`. None uses `Depends(get_db_conn)`. A contract test asserts
no handler in `routers/scenarios.py` depends on the pooled connection — the
failure it prevents is a 200 response with nothing written.

### CLI

```bash
scenario create --name "Centralidad norte" --aoi-id <uuid>
scenario list
scenario show --scenario-id <uuid>     # edits, overrides, last materialised
scenario add-edit --scenario-id <uuid> --geojson area.geojson \
    --levels 6 --unit-size 60 --people-per-household 3.6
scenario materialize --scenario-id <uuid>    # reports matched counts per edit
evaluate-scenario --scenario-id <uuid> --aoi-id <uuid>
```

`--levels` writes both `floors` and `num_levels`: they are the same quantity
under two names (`seed_demo_attributes.py:23-40`), and two independent fields
would leave `planned_floors` on the baseline while edificability moved, so one
proposal would make the map contradict itself. The number written to both keys
is the number the user typed, which is why this is not the silent default the
anti-goals forbid.

A scenario sets **all three** policy inputs, because `edificability.py:101-107`
skips any feature missing any of them.

### API

`GET`/`POST /scenarios`, `GET`/`DELETE /scenarios/{id}`,
`POST`/`DELETE /scenarios/{id}/edits`, `POST /scenarios/{id}/materialize`.
New `apps/api/routers/scenarios.py`; writes per "Transactions" above.

**`GET /indicators` grows `scenario_id`.** `list_indicators` filters to
`scenario_id IS NULL` on the stated grounds that a what-if value is not a
reason to offer an indicator on the baseline map. Right for the baseline and
fatal here — the five revived indicators have rows *only* under the scenario,
so the picker would never offer them.

⚠ Use `scenario_id = X OR scenario_id IS NULL` inside the probe. It is an
`EXISTS`, which emits no rows, so the double-counting the anti-join prevents
cannot occur; the anti-join would buy a correlated `NOT EXISTS` per catalog
row for nothing. *(The first draft prescribed "an r043-style overlay" and cited
a constraint that does not apply here.)* `SCENARIOS_LAYER.md`'s baseline-only
note must be amended.

### Frontend

- `selection.ts`'s `Feature<Polygon>` becomes the edit geometry unchanged.
- A scenario picker listing the active AOI's scenarios plus `(baseline)`.
- One form: levels, unit size, people per household — validated against the
  density ceiling before submit.
- **`activateIndicator` (`choropleth.ts:163-181`) must pass `scenario_id`**
  into the tile URL beside `aoi_id` at `:180`. *(The first draft named
  `renderIndicator`, which does not build the URL.)*
- **`listIndicators` (`api.ts:104`) takes only `aoiId`** and must carry
  `scenario_id` too, or the new parameter cannot be sent.

---

## What this does not give you

- **A scenario does not survive `DROP DATABASE`.** Export/import is not built.
- **No comparison view**, **no capacity model beyond the existing formula**,
  **no runtime tenant isolation**, **no author**.
- **Values only inside the drawn area, for the five revived indicators.**
  Outside it a block has no override, so edificability skips it, so there is
  no scenario row and no baseline row to fall back to. That is correct — those
  blocks have no proposal — but the map will show a painted patch in a blank
  AOI, and the UI should say so rather than let it read as a rendering fault.

## The risk I am not closing

**Materialisation cost at city scale is unmeasured.** A rule over 19,058
blocks writes up to one override row per block *per attribute* — four of them
— and `publish_scenario_version` digests every row for an attribute. The
analogous membership pass measured nine minutes before `ST_Subdivide` brought
it to forty seconds, so the shape and the remedy are both known. Measure a
full-district edit before assuming the interaction can be synchronous; if it
cannot, submit it through the job layer rather than inventing a second path.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | later edits win; one `--levels` writes both keys; the density ceiling is derived from the catalog and refuses `levels > unit_size/5` |
| `tests/integration` | materialise is idempotent; a removed edit retires its version **and** supersedes its results; `ST_Covers` on centroid matches `count_features_within_polygon` for the same polygon; materialise refuses without an AOI; delete removes overrides, versions, results and materialisation rows; an invalid polygon is repaired, not raised on |
| `tests/contract` | `AUTHORABLE_ATTRIBUTES` is the derived set minus exactly three; no `scenarios.py` handler depends on `get_db_conn` |
| `tests/e2e` | draw, set three values, recompute, and see the five indicators appear in the picker and paint inside the drawn area |

⚠ **The proof test needs its own fixture.** "The five indicators produce rows
under a scenario on the synthetic territory" passes *today*, before any of this
exists, because `synthetic_territory.py:116-119` seeds all four attributes on
every block. The test must build attribute-free blocks with
`tests/integration/_helpers.insert_feature`, assert the five write nothing,
apply a scenario, and assert they then write values.

⚠ **The e2e assertion is "appear", not "change".** For the five target
indicators the baseline has no rows, so they go absent → present; every
indicator that *has* a baseline is unaffected by these attributes. Nothing
"changes" in the sense the first draft's wording implied.

## Maintenance checklist

1. Migration r048 — two tables, RLS policy, indexes, **and the `backing_kind`
   CHECK change**
2. `serving_geo` unchanged
3. `explore` gains a scenario view
4. CLI listing commands read from `explore`
5. Tests per the table above
6. Docs: `AGENTS.md`; `SCENARIOS_LAYER.md` (partial FK reversal, baseline-only
   picker, "results stay readable"); `TERMINOLOGY.md` ("every `scenario_id`
   column is a plain uuid" is no longer true); `ENGINEERING_NOTES.md:108-110`
   ("a scenario→project association that does not exist" — it now does);
   **`docs/delivery/03_database_and_api_reference.md`**, which is generated,
   hashed in `SHA256SUMS`, and guarded by
   `test_repository_structure.py:427-478` — regenerate it *and* the manifest
7. **`scripts/acceptance_run.sh:185-223`** mints its scenario from `uuid5` with
   no `app_core.scenario` row, which stays legal and would make
   `explore.scenario_v` report zero scenarios immediately after the Schedule A
   proof run creates one. Move it onto the new CLI, which also exercises the
   new path in the acceptance artifact.

## Left to the builder

- Request and response shapes.
- Whether `materialize` runs synchronously or through the job layer — decide
  from the measurement the risk section calls for.
- Sidebar layout.
- Whether `explore.scenario_v` exposes edit counts, override counts, or both.

---

## What the reviews changed

Recorded because the first draft read as finished and was not. A builder
review found eight gaps; an adversarial review then found fifteen more, four of
which I verified against the code myself before accepting any of them.

**Would have shipped silent failures:**

- Every write endpoint would have returned success and persisted nothing —
  `get_db_conn` rolls back on the success path, a trap the repo documents three
  times.
- Removing an edit would have removed the override and left the result on the
  map, because supersession is scoped to the rows being written.
- Deleting an AOI would have silently widened a scoped rule to the whole
  database.
- Forgetting to materialise would have left the asset graph reporting
  everything fresh.

**Would have failed at runtime:** levels above `unit_size / 5` raise a plpgsql
exception that aborts every block at once, because `valid_max` is enforced and
area cancels out of the density formula.

**Was factually wrong in the first draft:** the `ST_CoveredBy` justification
(the cited precedent says the opposite); "every indicator endpoint already
takes `scenario_id`"; `renderIndicator` as the tile-URL site; "six readers"
(five, plus the tile); an RLS test that cannot pass because the test role owns
the tables; a proof test that passed before the feature existed.
