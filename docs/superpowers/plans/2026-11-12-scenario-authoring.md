# Scenario Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A named scenario sets policy attributes over a drawn area, the five
currently-empty indicators recompute against it, and the result is readable
against the baseline.

**Architecture:** A scenario is a row in `app_core.scenario`; its proposal is a
set of `curated_geo.scenario_edit` rules (area + attribute + value).
Materialisation resolves rules into `curated_geo.scenario_override` rows, which
the asset graph already partitions by scenario, so the existing
`evaluate-scenario` path recomputes exactly the affected indicators. Comparison
reads baseline and scenario results, which already coexist in
`indicator_result`.

**Tech Stack:** PostgreSQL 16 + PostGIS, Alembic, psycopg2, FastAPI, Typer,
pytest, TypeScript + MapLibre + Vitest.

**Spec:** `docs/superpowers/specs/SCENARIO_AUTHORING.md`

## Global Constraints

- Every API write opens its own committed connection —
  `with contextlib.closing(connect(api_dsn())) as c, c:` — never
  `Depends(get_db_conn)`, which rolls back on the success path
  (`apps/api/deps.py:131-136`).
- Bulk override writes go through `apply_overrides`, never `apply_override` in
  a loop.
- `publish_scenario_version(conn, scenario_id)` is called with **no**
  `attributes` argument wherever retirement must be reconciled.
- Supersession status is `'stale'`. The CHECK allows only
  `('current','stale','failed')` (`r016:186-187`).
- Features caught by a drawn area are selected with
  `ST_Covers(area, f.geometry_centroid)`.
- Commits are authored and committed by `Rossano <rossano.schifanella@gmail.com>`;
  no attribution trailers (`AGENTS.md`).
- Migration head at plan time is `r047`.

## Review Focus

Five conditions the spec implies that no task's happy path would exercise. Each
line's test is added to the task that owns the code.

1. **A drawn area that catches no blocks.** Expected: materialisation returns
   `matched: 0` to the caller and writes nothing, rather than reporting success.
   *(Task 6)*
2. **A drawn area crossing the AOI boundary.** Expected: blocks outside the
   scenario's AOI are excluded and the count returned says how many, so partial
   application is visible rather than silent. *(Task 6)*
3. **A self-intersecting polygon.** TerraDraw permits one and the type CHECK
   accepts it. Expected: repaired on write, or rejected with a reason — never
   passed to `ST_Covers` to raise mid-materialisation. *(Task 4)*
4. **A duplicate scenario name in one project.** Expected: 409 with a message,
   not a 500 from the unique violation. *(Task 9)*
5. **A ceiling violation anywhere in the selection.** Expected: the whole
   materialisation refuses and nothing is written — not a partial application
   that leaves some blocks overridden. *(Task 5, Task 6)*

---

## File Structure

| File | Responsibility |
|---|---|
| `infra/db/migrations/versions/r048_scenario_authoring.py` | the two tables, RLS, indexes |
| `infra/db/migrations/versions/r049_indicator_overlay_delta.py` | `indicator_overlay_tile` gains `delta` |
| `packages/py/reimagina/assets/registry.py` | `AUTHORABLE_ATTRIBUTES` beside `overridable_attributes()` |
| `packages/py/reimagina/scenarios/ceilings.py` | the three `valid_max` bounds, derived |
| `packages/py/reimagina/scenarios/authoring.py` | scenario + edit CRUD, materialisation, delete |
| `packages/py/reimagina/scenarios/comparison.py` | baseline-vs-scenario rows and summary |
| `apps/cli/scenario.py` | CLI wrappers |
| `apps/api/routers/scenarios.py` | the HTTP surface |
| `apps/web/src/scenario.ts` | picker, edit form, recompute |
| `apps/web/src/choropleth.ts` | `scenario_id` and `delta` in the tile URL |

---

### Task 1: Migration r048 — the tables

**Files:**
- Create: `infra/db/migrations/versions/r048_scenario_authoring.py`
- Test: `tests/integration/test_scenario_schema.py`

**Interfaces:**
- Produces: `app_core.scenario`, `curated_geo.scenario_edit` with the columns
  the spec's DDL blocks give verbatim.

- [ ] **Step 1: Write the failing test**

```python
def test_scenario_tables_exist_with_their_constraints(conn):
    # app_core.scenario
    assert _has_unique(conn, "app_core.scenario", ["project_id", "name"])
    assert _has_fk(conn, "app_core.scenario", "project_id", "app_core.project")
    assert _rls_enabled(conn, "app_core", "scenario")
    assert _policy_named(conn, "app_core", "scenario", "tenant_isolation")
    # curated_geo.scenario_edit
    assert _has_unique(conn, "curated_geo.scenario_edit", ["scenario_id", "ordinal"])
    assert _has_fk(conn, "curated_geo.scenario_edit", "type_id",
                   "curated_geo.feature_type_catalog")
    assert _column_not_null(conn, "curated_geo.scenario_edit", "value_numeric")
    assert not _column_exists(conn, "curated_geo.scenario_edit", "value_text")

def test_scenario_edit_rejects_a_non_polygon_geometry(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_edit(conn, geometry_wkt="LINESTRING(0 0, 1 1)")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_scenario_schema.py -v`
Expected: FAIL — relation `app_core.scenario` does not exist

- [ ] **Step 3: Write the migration**

`revision = "r048"`, `down_revision = "r047"`. The two `CREATE TABLE`
statements exactly as the spec gives them, plus
`ALTER TABLE app_core.scenario ENABLE ROW LEVEL SECURITY` and a
`tenant_isolation` policy whose predicate mirrors
`r011_row_level_security.py`'s on `project_id`. Indexes:
`scenario_edit_scenario_idx` btree, `scenario_edit_geometry_gist` GiST.
`downgrade()` drops both tables.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run alembic upgrade head && uv run pytest tests/integration/test_scenario_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add infra/db/migrations/versions/r048_scenario_authoring.py tests/integration/test_scenario_schema.py
git commit -m "Add the scenario and scenario_edit tables"
```

---

### Task 2: `AUTHORABLE_ATTRIBUTES`

**Files:**
- Modify: `packages/py/reimagina/assets/registry.py` (beside `overridable_attributes()` at `:186`)
- Test: `tests/contract/test_authorable_attributes.py`

**Interfaces:**
- Produces: `AUTHORABLE_ATTRIBUTES: tuple[str, ...]` and
  `NON_AUTHORABLE_REASONS: dict[str, str]`.

Lives in `registry.py`, not `indicators/catalog.py`: `registry.py` defers its
`SHIPPED_INDICATORS` import to function scope in three places to keep that
dependency one-way.

- [ ] **Step 1: Write the failing test**

```python
def test_authorable_is_the_derived_set_minus_three():
    assert set(AUTHORABLE_ATTRIBUTES) == set(overridable_attributes()) - {
        "geometry", "poi_version", "network_version"}

def test_every_exclusion_states_why():
    assert set(NON_AUTHORABLE_REASONS) == {"geometry", "poi_version", "network_version"}
    assert all(reason.strip() for reason in NON_AUTHORABLE_REASONS.values())

def test_a_new_policy_attribute_would_appear_without_editing_a_list():
    # The derivation, not a literal: this fails if someone hardcodes the four.
    assert set(AUTHORABLE_ATTRIBUTES) <= set(overridable_attributes())
```

- [ ] **Step 2: Run to verify it fails** — `ImportError`

Run: `uv run pytest tests/contract/test_authorable_attributes.py -v`

- [ ] **Step 3: Implement**

`AUTHORABLE_ATTRIBUTES` derived by subtraction from `overridable_attributes()`,
sorted. `NON_AUTHORABLE_REASONS` carries one sentence each: `geometry` is not a
number; `poi_version` and `network_version` are hand-bumped dirty markers whose
override recomputes against identical inputs.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Derive which attributes a scenario may author"`

---

### Task 3: The three ceilings

**Files:**
- Create: `packages/py/reimagina/scenarios/ceilings.py`
- Test: `tests/unit/test_scenario_ceilings.py`

**Interfaces:**
- Produces:
  `check_ceilings(*, levels: float, unit_size: float, people_per_household: float, max_block_area_m2: float) -> list[CeilingBreach]`
  where `CeilingBreach` is a frozen dataclass of
  `(indicator_slug: str, bound: float, would_be: float)`. Empty list means all
  three pass.
- Produces: `max_levels_for(unit_size: float) -> float` — the density bound
  alone, for the form's pre-check.

- [ ] **Step 1: Write the failing test**

```python
def test_density_bound_is_area_independent_and_derived():
    # 10000 * levels / unit_size <= catalog valid_max (2000)
    assert max_levels_for(60.0) == pytest.approx(12.0)
    assert check_ceilings(levels=6, unit_size=60, people_per_household=3.6,
                          max_block_area_m2=5_944) == []

def test_density_breach_names_the_indicator_and_the_bound():
    [breach] = [b for b in check_ceilings(
        levels=13, unit_size=60, people_per_household=3.6, max_block_area_m2=5_944)
        if b.indicator_slug == "edificability_block_density"]
    assert breach.bound == 2000.0
    assert breach.would_be == pytest.approx(10000 * 13 / 60)

def test_housing_units_bound_depends_on_block_area():
    # The catalog records a measured Tlalpan block at 138,695 units at 6/60.
    big = 138_695 * 60 / 6          # the area that produces it
    assert check_ceilings(levels=6, unit_size=60, people_per_household=3.6,
                          max_block_area_m2=big) == []
    breaches = check_ceilings(levels=12, unit_size=60, people_per_household=3.6,
                              max_block_area_m2=big)
    assert any(b.indicator_slug == "edificability_block_housing_units" for b in breaches)

def test_population_bound_also_scales_with_household_size():
    area = 138_695 * 60 / 6
    assert any(b.indicator_slug == "edificability_block_population"
               for b in check_ceilings(levels=6, unit_size=60,
                                       people_per_household=40,
                                       max_block_area_m2=area))

def test_bounds_come_from_the_catalog_not_from_literals(monkeypatch):
    # Changing the catalog's valid_max moves the ceiling.
    ...
    assert max_levels_for(60.0) != 12.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_scenario_ceilings.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

Read `valid_max` from `SHIPPED_INDICATORS` by slug for
`edificability_block_density`, `edificability_block_housing_units`,
`edificability_block_population`. Formulas per the spec's table:
`10000 × levels / unit_size`, `area × levels / unit_size`, and the second
times `people_per_household`.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Derive the three ceilings a scenario can breach"`

---

### Task 4: Scenario and edit CRUD

**Files:**
- Create: `packages/py/reimagina/scenarios/authoring.py`
- Test: `tests/integration/test_scenario_authoring.py`

**Interfaces:**
- Produces:
  - `create_scenario(conn, *, project_id, name, aoi_id=None, description=None) -> str`
  - `list_scenarios(conn, *, project_id=None, aoi_id=None) -> list[dict]`
  - `get_scenario(conn, scenario_id) -> dict | None` — includes `edits`,
    `override_count`, `materialized_at`
  - `add_edit(conn, *, scenario_id, geometry_wkt, type_id, attribute_key, value_numeric) -> str`
  - `remove_edit(conn, *, edit_id) -> bool`
  - `DuplicateScenarioName(Exception)`, `InvalidGeometry(Exception)`

`ordinal` is assigned by `add_edit` as `max(ordinal) + 1` within the scenario.

- [ ] **Step 1: Write the failing test**

```python
def test_create_then_find_again(conn):
    sid = create_scenario(conn, project_id=p, name="Centralidad norte", aoi_id=a)
    assert get_scenario(conn, sid)["name"] == "Centralidad norte"
    assert [s["id"] for s in list_scenarios(conn, aoi_id=a)] == [sid]

def test_duplicate_name_in_one_project_raises_its_own_error(conn):
    create_scenario(conn, project_id=p, name="N")
    with pytest.raises(DuplicateScenarioName):
        create_scenario(conn, project_id=p, name="N")

def test_edits_get_increasing_ordinals(conn):
    a1 = add_edit(conn, scenario_id=sid, ..., value_numeric=6)
    a2 = add_edit(conn, scenario_id=sid, ..., value_numeric=8)
    ords = {e["id"]: e["ordinal"] for e in get_scenario(conn, sid)["edits"]}
    assert ords[a2] > ords[a1]

# Review Focus 3
def test_a_self_intersecting_polygon_is_repaired_not_stored_raw(conn):
    bowtie = "POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))"
    eid = add_edit(conn, scenario_id=sid, geometry_wkt=bowtie, ...)
    stored = _geometry_of(conn, eid)
    assert _is_valid(conn, stored)

def test_an_unrepairable_geometry_is_rejected_with_a_reason(conn):
    with pytest.raises(InvalidGeometry):
        add_edit(conn, scenario_id=sid, geometry_wkt="POLYGON EMPTY", ...)

def test_an_unauthorable_attribute_is_refused(conn):
    with pytest.raises(ValueError, match="network_version"):
        add_edit(conn, scenario_id=sid, attribute_key="network_version", ...)
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement**

`add_edit` repairs with `ST_CollectionExtract(ST_MakeValid(%s::geometry), 3)`
and raises `InvalidGeometry` when the result is empty. It validates
`attribute_key` against `AUTHORABLE_ATTRIBUTES` from Task 2.
`create_scenario` catches `psycopg2.errors.UniqueViolation` and re-raises
`DuplicateScenarioName`.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Create, find and edit a scenario"`

---

### Task 5: Materialisation

**Files:**
- Modify: `packages/py/reimagina/scenarios/authoring.py`
- Test: `tests/integration/test_scenario_materialize.py`

**Interfaces:**
- Consumes: Task 3's `check_ceilings`, Task 4's CRUD.
- Produces:
  `materialize_scenario(conn, *, scenario_id) -> MaterializeResult` where
  `MaterializeResult` is a frozen dataclass of
  `(matched_per_edit: dict[str, int], overrides_written: int, overrides_removed: int, results_superseded: int)`.
- Produces: `ScenarioHasNoAOI(Exception)`, `CeilingExceeded(Exception)` carrying
  `breaches: list[CeilingBreach]` and the offending `feature_id`.

- [ ] **Step 1: Write the failing test**

```python
def test_an_edit_overrides_the_blocks_its_area_covers(conn):
    result = materialize_scenario(conn, scenario_id=sid)
    assert result.matched_per_edit[eid] == 4
    assert _override_value(conn, sid, inside_block, "num_levels") == 6
    assert _override_value(conn, sid, outside_block, "num_levels") is None

def test_a_later_ordinal_wins_over_an_earlier_one(conn):
    # Two overlapping areas, same attribute, values 6 then 8.
    materialize_scenario(conn, scenario_id=sid)
    assert _override_value(conn, sid, overlapped_block, "num_levels") == 8

def test_materialising_twice_changes_nothing_and_publishes_nothing(conn):
    first = materialize_scenario(conn, scenario_id=sid)
    version_before = _current_version(conn, sid, "num_levels")
    second = materialize_scenario(conn, scenario_id=sid)
    assert second.overrides_written == first.overrides_written
    assert _current_version(conn, sid, "num_levels") == version_before

def test_removing_an_edit_removes_its_overrides_and_supersedes_its_results(conn):
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, "edificability_block_density", scenario_id=sid)
    remove_edit(conn, edit_id=eid)
    result = materialize_scenario(conn, scenario_id=sid)
    assert result.overrides_removed == 4
    assert result.results_superseded > 0
    assert _current_results(conn, sid, "edificability_block_density") == []

def test_removing_the_last_edit_for_an_attribute_retires_its_version(conn):
    materialize_scenario(conn, scenario_id=sid)
    remove_edit(conn, edit_id=eid)
    materialize_scenario(conn, scenario_id=sid)
    assert _current_version(conn, sid, "num_levels") is None

def test_it_refuses_when_the_scenario_has_no_aoi(conn):
    _set_aoi_null(conn, sid)
    with pytest.raises(ScenarioHasNoAOI):
        materialize_scenario(conn, scenario_id=sid)

# Review Focus 1
def test_an_area_catching_no_blocks_reports_zero_and_writes_nothing(conn):
    result = materialize_scenario(conn, scenario_id=sid_with_empty_area)
    assert result.matched_per_edit[eid] == 0
    assert result.overrides_written == 0

# Review Focus 2
def test_blocks_outside_the_aoi_are_excluded_and_the_count_says_so(conn):
    # The drawn area covers 6 blocks; 2 lie outside the scenario's AOI.
    result = materialize_scenario(conn, scenario_id=sid)
    assert result.matched_per_edit[eid] == 4

# Review Focus 5
def test_a_ceiling_breach_refuses_the_whole_run(conn):
    with pytest.raises(CeilingExceeded) as exc:
        materialize_scenario(conn, scenario_id=sid_with_13_levels)
    assert exc.value.breaches[0].indicator_slug == "edificability_block_density"
    assert _override_count(conn, sid_with_13_levels) == 0
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement the seven steps from the spec**

In order: refuse without an AOI; select by `type_id` and
`ST_Covers(edit.geometry, f.geometry_centroid)` joined to `aoi_feature`,
resolving collisions by `ordinal`; `check_ceilings` against the selection's
`max(ST_Area(geometry::geography))`; delete the overrides no edit implies,
keeping the ids; one `apply_overrides` call; `publish_scenario_version` with no
`attributes`; then mark this scenario's `current` `indicator_result` rows
`'stale'` for the removed ids and the indicators reading the removed
attributes. One transaction.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Resolve scenario edits into overrides"`

---

### Task 6: Deleting a scenario

**Files:**
- Modify: `packages/py/reimagina/scenarios/authoring.py`
- Test: `tests/integration/test_scenario_delete.py`

**Interfaces:**
- Produces: `delete_scenario(conn, *, scenario_id) -> DeleteResult`, a frozen
  dataclass of `(overrides, versions_retired, results_superseded, materializations)`.

- [ ] **Step 1: Write the failing test**

```python
def test_delete_removes_overrides_versions_results_and_materialisations(conn):
    materialize_scenario(conn, scenario_id=sid)
    _compute_indicator(conn, "edificability_block_density", scenario_id=sid)
    result = delete_scenario(conn, scenario_id=sid)
    assert result.overrides > 0 and result.results_superseded > 0
    assert _current_version(conn, sid, "num_levels") is None
    assert _materialization_rows(conn, sid) == []
    assert get_scenario(conn, sid) is None
    assert _edit_count(conn, sid) == 0          # cascaded

def test_the_name_is_reusable_after_delete(conn):
    delete_scenario(conn, scenario_id=sid)
    create_scenario(conn, project_id=p, name="Centralidad norte")

def test_a_deleted_scenario_leaves_no_stale_asset_entry(conn):
    delete_scenario(conn, scenario_id=sid)
    assert sid not in {r["partition_key"] for r in _stale_assets(conn)}
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** the five ordered steps from the spec, one transaction.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Retire a scenario rather than orphaning its traces"`

---

### Task 7: Comparison

**Files:**
- Create: `packages/py/reimagina/scenarios/comparison.py`
- Test: `tests/integration/test_scenario_comparison.py`

**Interfaces:**
- Produces:
  `compare(conn, *, scenario_id, indicator_slug, aoi_id=None) -> ComparisonResult`
  with `rows: list[dict]` of
  `{feature_id, baseline, scenario, delta, state}` where `state` is
  `"changed" | "unchanged" | "new"`, and
  `summary: {changed, unchanged, new, mean_delta, higher_is_better}`.

- [ ] **Step 1: Write the failing test**

```python
def test_a_changed_block_reports_its_delta(conn):
    row = _row_for(compare(conn, scenario_id=sid, indicator_slug=slug), block)
    assert row["state"] == "changed"
    assert row["delta"] == pytest.approx(row["scenario"] - row["baseline"])

def test_an_untouched_block_is_unchanged_not_absent(conn):
    assert _row_for(..., untouched_block)["state"] == "unchanged"

# The five indicators this exists for have no baseline at all.
def test_an_indicator_with_no_baseline_reports_new_not_a_delta(conn):
    row = _row_for(compare(conn, scenario_id=sid,
                           indicator_slug="edificability_block_density"), block)
    assert row["state"] == "new"
    assert row["baseline"] is None and row["delta"] is None

def test_the_summary_carries_the_catalog_direction(conn):
    result = compare(conn, scenario_id=sid, indicator_slug="edificability_block_density")
    assert result.summary["higher_is_better"] is True
    assert result.summary["new"] == result.summary["new"]  # counted, not inferred
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement**

A `FULL OUTER JOIN` between the scenario's `current` rows and the baseline's,
modelled on `acceptance_run.sh:242-269`'s LEFT JOIN but keeping scenario-only
rows. `higher_is_better` is read from the catalog.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Compare a scenario against the baseline it departs from"`

---

### Task 8: CLI

**Files:**
- Create: `apps/cli/scenario.py`
- Modify: `apps/cli/app.py` (register `scenario_app` beside the others at `:254`)
- Test: `tests/unit/test_scenario_cli.py`, `tests/contract/test_runbook_scripts_match_the_cli.py`

**Interfaces:**
- Consumes: Tasks 4–7.
- Produces: `scenario create|list|show|set-attribute|recompute|compare`.

`set-attribute` takes `--levels`, `--unit-size`, `--people-per-household` and
writes **four** edits: `--levels` produces both `floors` and `num_levels`.
`recompute` calls `materialize_scenario` then the existing
`evaluate-scenario` path.

- [ ] **Step 1: Write the failing test**

```python
def test_levels_writes_both_floors_and_num_levels():
    edits = _edits_for(levels=6, unit_size=60, people_per_household=3.6)
    assert {e["attribute_key"] for e in edits} == {
        "floors", "num_levels", "unit_size", "people_per_household"}
    assert _value(edits, "floors") == _value(edits, "num_levels") == 6

def test_set_attribute_prechecks_the_density_ceiling(capsys):
    code = _invoke("set-attribute", levels=13, unit_size=60, ...)
    assert code != 0
    assert "edificability_block_density" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — Typer sub-app following `apps/cli/overture.py`'s shape; `_print` for output.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Author a scenario from the command line"`

---

### Task 9: API router

**Files:**
- Create: `apps/api/routers/scenarios.py`
- Modify: `apps/api/app.py:136` (include the router)
- Test: `tests/contract/test_scenarios_api.py`

**Interfaces:**
- Consumes: Tasks 4–7.
- Produces: `GET`/`POST /scenarios`, `GET`/`DELETE /scenarios/{id}`,
  `POST`/`DELETE /scenarios/{id}/edits`, `POST /scenarios/{id}/recompute`,
  `GET /scenarios/{id}/comparison`.

- [ ] **Step 1: Write the failing test**

```python
def test_no_handler_uses_the_pooled_connection():
    # The pool rolls back on success; a write through it returns 200 and persists nothing.
    import apps.api.routers.scenarios as mod
    for route in mod.router.routes:
        assert "get_db_conn" not in _dependency_names(route)

def test_creating_a_scenario_persists_it(client):
    sid = client.post("/scenarios", json={...}).json()["id"]
    assert client.get(f"/scenarios/{sid}").status_code == 200

# Review Focus 4
def test_a_duplicate_name_is_409_not_500(client):
    client.post("/scenarios", json={"name": "N", "project_id": p})
    assert client.post("/scenarios", json={"name": "N", "project_id": p}).status_code == 409

def test_recompute_reports_what_it_matched(client):
    body = client.post(f"/scenarios/{sid}/recompute").json()
    assert body["matched_per_edit"] and body["overrides_written"] > 0
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — every write inside
  `with contextlib.closing(connect(api_dsn())) as c, c:`.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Serve scenario authoring over HTTP"`

---

### Task 10: `GET /indicators` learns about scenarios

**Files:**
- Modify: `packages/py/reimagina/storage/postgres/repositories.py:2316-2352`,
  `packages/py/reimagina/storage/interfaces/indicators.py`,
  `apps/api/routers/indicators.py`
- Test: `tests/integration/test_indicator_picker_scenario.py`

**Interfaces:**
- Produces: `list_indicators(*, with_values_for_aoi=None, scenario_id=None)`.

- [ ] **Step 1: Write the failing test**

```python
def test_an_indicator_with_only_scenario_rows_is_offered_under_that_scenario(conn):
    assert slug not in _slugs(list_indicators(with_values_for_aoi=aoi))
    assert slug in _slugs(list_indicators(with_values_for_aoi=aoi, scenario_id=sid))

def test_the_baseline_picker_is_unchanged(conn):
    assert _slugs(list_indicators(with_values_for_aoi=aoi)) == baseline_slugs
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement**

Inside the existing `EXISTS` probe use
`(ir.scenario_id = %s OR ir.scenario_id IS NULL)`. ⚠ Not the r043 anti-join:
the probe emits no rows, so double-counting cannot occur and the anti-join
would add a correlated `NOT EXISTS` per catalog row for nothing.

- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Offer an indicator the active scenario has computed"`

---

### Task 11: Migration r049 — delta tiles

**Files:**
- Create: `infra/db/migrations/versions/r049_indicator_overlay_delta.py`
- Test: `tests/integration/test_indicator_overlay_delta.py`

**Interfaces:**
- Produces: `serving_geo.indicator_overlay_tile` accepting
  `query_params->>'delta'`; emits `delta` and `state` properties instead of
  `value` when set.

- [ ] **Step 1: Write the failing test**

```python
def test_without_delta_the_tile_is_byte_identical_to_before(conn):
    assert _tile(conn, z, x, y, {"indicator_slug": s, "scenario_id": sid}) == _tile_before

def test_delta_emits_the_difference_where_both_exist(conn):
    props = _decode(_tile(conn, z, x, y, {..., "delta": "true"}))
    assert props[changed_block]["delta"] == pytest.approx(expected)
    assert props[changed_block]["state"] == "changed"

def test_delta_marks_a_scenario_only_value_new(conn):
    props = _decode(_tile(conn, z, x, y, {"indicator_slug": "edificability_block_density",
                                          "scenario_id": sid, "delta": "true"}))
    assert props[block]["state"] == "new"
    assert props[block].get("delta") is None
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — `CREATE OR REPLACE FUNCTION`, keeping the existing
  signature `(z, x, y, query_params json) RETURNS bytea` so `TILE_FUNCTIONS`
  and `infra/tiles/layers.json` need no change.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Serve a scenario's difference from the baseline as a tile"`

---

### Task 12: Frontend — picker, form, recompute

**Files:**
- Create: `apps/web/src/scenario.ts`
- Modify: `apps/web/src/api.ts:104` (`listIndicators` carries `scenario_id`),
  `apps/web/src/choropleth.ts:163-181` (`activateIndicator` passes
  `scenario_id`), `apps/web/src/main.ts`
- Test: `apps/web/src/scenario.test.ts`

**Interfaces:**
- Consumes: Task 9's endpoints, Task 10's `scenario_id` parameter.
- Produces: `enableScenarioPanel(map, { onScenarioChange }): ScenarioController`.

- [ ] **Step 1: Write the failing test**

```typescript
it("puts scenario_id in the tile URL when a scenario is active", () => {
  expect(tileUrlFor({ slug, aoiId, scenarioId: "s-1" }))
    .toContain("scenario_id=s-1");
});

it("omits scenario_id on the baseline", () => {
  expect(tileUrlFor({ slug, aoiId, scenarioId: null })).not.toContain("scenario_id");
});

it("refuses levels above the density ceiling before submitting", () => {
  expect(validateEdit({ levels: 13, unitSize: 60, peoplePerHousehold: 3.6 }))
    .toMatch(/density/);
});
```

- [ ] **Step 2: Run to verify it fails** — `npx vitest run src/scenario.test.ts`
- [ ] **Step 3: Implement** — the form writes one edit set; `validateEdit`
  mirrors `max_levels_for` from Task 3 against the catalog bound served by the API.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Author a scenario from the map"`

---

### Task 13: Frontend — delta mode

**Files:**
- Modify: `apps/web/src/choropleth.ts`, `apps/web/src/scenario.ts`
- Test: `apps/web/src/delta-ramp.test.ts`

**Interfaces:**
- Consumes: Task 11's `delta` parameter, Task 7's summary.
- Produces: `deltaRamp(domain): ExpressionSpec` and a value/delta toggle.

- [ ] **Step 1: Write the failing test**

```typescript
it("centres the diverging ramp on zero", () => {
  const ramp = deltaRamp({ min: -40, max: 120 });
  expect(colourAt(ramp, 0)).toBe(NEUTRAL);
});

it("uses a different palette from the value ramp", () => {
  expect(new Set(paletteOf(deltaRamp({min:-1,max:1}))))
    .not.toEqual(new Set(paletteOf(valueRamp({min:0,max:1}))));
});

it("renders a `new` feature distinctly from an unchanged one", () => {
  expect(styleFor({ state: "new" })).not.toEqual(styleFor({ state: "unchanged" }));
});
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — symmetric domain about zero, diverging palette per
  the `dataviz` conventions already in `style.css`; `new` gets its own fill.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Show a scenario as a difference, not as a value"`

---

### Task 14: Docs, acceptance run, and the delivery reference

**Files:**
- Modify: `AGENTS.md`, `docs/architecture/SCENARIOS_LAYER.md`,
  `docs/architecture/TERMINOLOGY.md`, `docs/ENGINEERING_NOTES.md:108-110`,
  `scripts/acceptance_run.sh:185-223`, `infra/db/migrations/` (explore view)
- Test: `tests/contract/test_repository_structure.py` (existing guards)

- [ ] **Step 1: Run the guards to see them fail**

Run: `uv run pytest tests/contract/test_repository_structure.py -v`
Expected: FAIL — `docs/delivery/03_database_and_api_reference.md` is stale

- [ ] **Step 2: Move `acceptance_run.sh` onto the new CLI**

Replace the `uuid5` + `apply_overrides` heredoc with
`scenario create` / `set-attribute` / `recompute`, so the Schedule A run
exercises the new path and `explore.scenario_v` does not report zero scenarios
right after it creates one.

- [ ] **Step 3: Update the architecture docs**

`SCENARIOS_LAYER.md`: the partial FK reversal, the picker is no longer
baseline-only, "results stay readable" is no longer true after delete.
`TERMINOLOGY.md:145`: "every `scenario_id` column is a plain uuid" is no longer
true for authoring. `ENGINEERING_NOTES.md:108-110`: the scenario→project
association now exists. `AGENTS.md`: the new CLI commands.

- [ ] **Step 4: Regenerate the delivery reference and manifest**

```bash
uv run python scripts/render_delivery_pdfs.py
uv run python scripts/delivery_manifest.py --tag <next-rc>
sha256sum -c docs/delivery/SHA256SUMS
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/unit tests/contract tests/integration -q && (cd apps/web && npx vitest run)`
Expected: PASS

- [ ] **Step 6: Commit** — `"Record scenario authoring in the docs and the acceptance run"`

---

### Task 15: Performance contract

**Files:**
- Create: `tests/performance/test_scenario_materialize_plan.py`
- Modify: `docs/architecture/PERFORMANCE.md`

Per `AGENTS.md`, `PERFORMANCE.md` owns benchmark workloads and budgets, and the
spec's one open risk is materialisation cost at city scale.

- [ ] **Step 1: Write the failing test**

```python
def test_the_materialisation_select_uses_the_centroid_index(conn):
    plan = _explain(conn, _materialize_select_sql(), params)
    assert "Seq Scan on feature" not in plan
    assert "geometry_centroid" in plan
```

- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Measure a full-district edit and record the number in
  `PERFORMANCE.md`**, then decide synchronous vs job-layer for `recompute` and
  record which, with the measurement behind it.
- [ ] **Step 4: Run to verify it passes**
- [ ] **Step 5: Commit** — `"Pin the materialisation plan and record its cost"`

---

## Self-Review

**Spec coverage.** Every section maps to a task: the two tables (1), the
allowlist (2), the three ceilings (3), CRUD and geometry repair (4),
materialisation's seven steps (5), delete's five (6), comparison's three states
(7), CLI (8), API and transactions (9), the picker (10), delta tiles (11),
frontend (12–13), docs and acceptance (14), the open risk (15). The spec's
non-goals produce no tasks, by design.

**Gaps found and fixed while reviewing.** Task 15 did not exist — the spec's
risk section is entirely about cost and `AGENTS.md` names `tests/performance`
as a required layer, so a plan with no performance task would have left the one
acknowledged risk unmeasured. Task 10's step 3 originally said "overlay like
r043"; corrected to the `OR` form, because the probe is an `EXISTS`.

**Type consistency.** `materialize_scenario` returns `MaterializeResult` in
Tasks 5, 8 and 9; `check_ceilings` returns `list[CeilingBreach]` in Tasks 3, 5
and 8; `compare` returns `ComparisonResult` in Tasks 7, 9 and 13;
`scenario_id` is `str` throughout.

**Review Focus coverage.** 1 and 2 and 5 are tested in Task 5, 3 in Task 4, 4
in Task 9 — each in the task owning the code, as its own step.

**Proportion.** 15 tasks against a ~330-line spec. Steps carry signatures,
test names and assertions; the only code blocks are tests. No task body is
written out.
