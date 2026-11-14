# Design: closing the Phase 2 contract gap

What the brief promised, what the platform delivers today, and the smallest
honest path from one to the other.

**Status:** design and plan, for approval. No code is written against this yet.

---

## Two caveats before anything else

**1. The brief is not in this repository.** The nearest authority in the tree
is `docs/legacy/reimagina_brief_review.md` — a design *review of* the brief,
which restates the promised journey and cites the brief's own sections (§3,
§3.1, §4.3, §5, §7.1, §7.6, §9.1, §9.2, §11). Everything below is measured
against that restatement.

> ⚠ If the signed brief says something the review does not, this document is
> wrong where they differ and must be re-based on the brief itself. I would
> rather flag that than quietly treat a review as the contract.

**2. The `superpowers` plugin is not installed in this session.** `ListPlugins`
returns nothing. So this follows the methodology's *shape* by hand — measure
before claiming, design before code, TDD, evidence before "done" — rather than
invoking skills that are not here.

---

## What was promised

A five-step user journey (`reimagina_brief_review.md:3-21`):

1. open a project; see the territory as it is — the **baseline**
2. create a **scenario** to explore a hypothesis
3. **sketch edits on the map** — move a lot, change a building's height, add a
   wall, add 200 buildings; each edit recorded as an **intervention**
4. click **Generate**; the platform resolves the territory and recomputes
   indicators on top of it
5. open the **compare panel** — baseline vs scenario, or scenario vs scenario

Resting on three stated rules:

- **R1** the intervention log is the source of truth; replaying it is
  deterministic; an Element is never overwritten in place
- **R2** a separate read model holds the resolved state; the compare panel
  reads only that projection
- **R3** AoI overlap is recomputed per scenario

Promised tables: `element`, `intervention`, `element_state`,
`element_aoi_state`, `element_aoi_baseline`, `scenario`,
`scenario_generation`.

---

## What we deliver now

| Brief stage | Delivered today | Evidence |
|---|---|---|
| 1 — author interventions | `curated_geo.scenario_override`: one row per `(scenario_id, feature_id, attribute_key)` → `value_numeric`/`value_text`. **Attribute-only, and with no write path of any kind.** | `r030_scenario_override.py:26-42`; `SCENARIOS_LAYER.md:284` |
| 2 — resolve scenario state | Resolved at **read time**, not materialised: `override ⊕ observation ⊕ base`. Works. | `scenarios/overrides.py:169-181`; `SCENARIOS_LAYER.md:179` |
| 3 — per-scenario AoI overlap | Not per-scenario. Scenarios never touch geometry, so overlap cannot change. | `SCENARIOS_LAYER.md:207-220` |
| 4 — per-Element indicators | Delivered, and well: 13 indicators, an asset graph that propagates a per-attribute dirty set, run orchestration. | `r041_asset_graph.py`; `r045_scenario_override_per_attribute.py`; `r043:8-12` |
| 5 — compare panel | API serves both sides (`?scenario_id=`); scenario tiles fall back to baseline. **No compare UI.** | `aois.py:96`, `indicators.py:233`; `r043`; `apps/web` has three pages — map, catalog, ops |

Promised tables that **do not exist and never did**: `intervention`,
`element_state`, `element_aoi_state`, `element_aoi_baseline`,
`scenario_generation`, and `scenario` itself. `app_core` contains only
`project` (`SCENARIOS_LAYER.md:69`, `r001:27`).

**The one-line summary: the engine is built and the cockpit is missing.**
Everything downstream of an edit works and is covered by 322 integration
tests — change-set derivation, per-attribute dirty propagation, the overlay,
run orchestration, indicator recompute, scenario-filtered reads and tiles.
What is absent is every way for a human to *cause* an edit.

---

## The four gaps, by contractual weight

**G1 — no write path *for a human*.** No API route and no CLI command writes
an override row, so brief stages 2 and 3 have no entry point and the journey
cannot be walked. This is the blocker and everything else is secondary.

But the gap is thinner than it looks, and this is the best news in this
document. `packages/py/reimagina/scenarios/overrides.py` already exposes a
tested Python write API — `apply_override` (`:26`), `apply_overrides` (`:62`)
and `publish_scenario_version` (`:85`) — exercised by **four** integration
tests: `test_scenario_whatif.py`, `test_edificability_pipeline.py`,
`test_asset_publish.py`, `test_freshness_driven_recompute.py`. Publishing an
asset version on edit is in there too. So Phase 1's API is a thin HTTP layer
over functions that already work, not new machinery.

> ⚠ Two claims in `SCENARIOS_LAYER.md` have gone stale and should be corrected
> in the same change: `:284` says the what-if test is "the only caller in the
> tree" (there are four), and `:452` cites `overrides.py:74-90` for effective
> resolution, which now lives at `:169-181`. Per the `AGENTS.md` maintenance
> checklist, a doc that misdescribes the read path is a maintenance failure,
> and this is exactly the kind of drift that makes inspection harder.

**G2 — no scenario entity.** `scenario_id` is a bare uuid with no table behind
it, so a scenario cannot be named, listed, forked or given a run history.
`NULL` means baseline. This was a recorded decision, not an oversight
(`r016:7-10`, `r030:5-6`).

**G3 — attribute-only.** The brief promises map sketching. Geometry lives in
`curated_geo.feature`, which a scenario structurally never edits, and the
override table has no geometry column and no way to add or remove a feature.

**G4 — no compare panel.** Stage 5 exists as SQL recipes and API parameters,
not as a UI.

---

## Design decisions

### D1 — Keep the override mechanism. Do not rebuild it as the brief's projection.

R2's `element_state` projection is a **performance** design: materialise so
compare is fast. The delivered read-time overlay already achieves the same
*semantics*. Materialising should follow a measured need, and
`docs/architecture/PERFORMANCE.md` owns those budgets.

Against rebuilding: the asset graph is keyed on one override asset per
attribute (`r045`), so replacing the write model means rewriting the dirty-set
engine that the integration suite covers. That is a large change with no
user-visible gain, taken while the journey still cannot be walked.

> This is a **deliberate deviation from R2** and is recorded here to be agreed
> with the client, not buried. Deviating from R1 is avoidable — see D3.

### D2 — Add `app_core.scenario` now.

The cheapest change that unblocks G2, and partly G1 and G4: it gives a
scenario a name, an owner, a `parent_scenario_id` for forking, and somewhere
to hang generation history. The FK was always meant to arrive with the table.

### D3 — Make the write path an append-only edit log, projected into the override table.

This is the load-bearing idea. Rather than choose between the brief's
intervention log and the delivered override table, write **both**:

- `app_core.scenario_edit` — append-only, one row per authored edit, never
  updated in place, replayable in order. This *is* R1, honoured literally.
- a small projector folds the log into `curated_geo.scenario_override`, which
  stays exactly as it is.

So R1 is satisfied, the engine downstream is untouched, and the log is the
seam through which geometry edits later arrive without a second migration of
the write model. An "undo" becomes a new row, not a delete.

### D4 — Geometry is its own phase, and must not ride along.

G3 needs a geometry-carrying edit, feature add/delete *within* a scenario, and
R3's per-scenario overlap recompute with the brief's fast/slow path
(`reimagina_brief_review.md:317-360`). That is the single largest piece of
remaining work in the contract. Bundling it with the write path would sink
both.

### D5 — The compare panel needs no new read API.

`/aois/{aoi_id}/summaries?scenario_id=` and
`/indicators/{slug}/values?scenario_id=` already serve either side, and `r043`
closed the tile holes. v1 of the panel is frontend plus a scenario list
endpoint.

---

## Plan

Phases are ordered so each one ends with something demonstrable. No phase
begins before the previous one's acceptance evidence exists.

### Phase 1 — the journey becomes walkable

1. `app_core.scenario` migration + `tests/integration` for the table and FK
2. `app_core.scenario_edit`, append-only, with a contract test asserting no
   `UPDATE`/`DELETE` path exists
3. the projector: log → `scenario_override`, unit-tested on replay
   determinism (same log, same overrides, any number of replays)
4. API: create scenario, list scenarios, author an edit, list a scenario's
   edits — the first write routes in the project, delegating to the existing
   `apply_override` / `publish_scenario_version` rather than reimplementing
   them
5. wire the existing run submit so authoring an edit can be followed by
   Generate
6. correct the two stale `SCENARIOS_LAYER.md` citations noted under G1

**Acceptance:** a test walks brief stages 1→4 end to end with no SQL fixture —
create scenario, author edit, submit run, read a changed indicator value. The
`test_scenario_whatif.py` fixture stops being the only writer.

### Phase 2 — the compare panel

6. scenario list in the UI; scenario picker on the map
7. compare view: baseline vs scenario for one AoI, from the existing endpoints
8. vitest coverage for the new components

**Acceptance:** brief stage 5 demonstrable in a browser, screenshotted.

### Phase 3 — geometry interventions

Its own spec, written after Phase 2 ships. Carries R3, the fast/slow overlap
path, and feature add/delete. Sized separately and honestly.

---

## What I am not proposing

- not rewriting the dirty-set engine
- not materialising `element_state` without a measured need
- not bundling geometry into the write path
- not touching `curated_geo.feature` or `observation` from a scenario — the
  baseline stays read-only, which is the one rule the delivery got right and
  kept

---

## Decisions I need from you

1. **Where is the signed brief?** This is measured against a review of it.
2. **Is attribute-only acceptable for the next milestone**, with geometry as a
   named Phase 3? That is a contractual call, not an engineering one.
3. **Do you accept the D1/D3 reconciliation** — R1 honoured literally via the
   edit log, R2 deviated from deliberately — or does the client expect the
   brief's shape built literally?
4. **Authentication.** The API has none: CORS and a request-id middleware only
   (`apps/api/app.py:110-111`). Phase 1 adds the first write routes to it. I
   will not ship a public, unauthenticated scenario-write endpoint; either auth
   lands inside Phase 1 or the write routes stay off the deployed surface
   until it does.
