# Open Issues and Design Decisions

Unresolved questions and gaps identified during architecture review. Each item needs a decision before the corresponding layer is implemented.

---

## 1. Spatial aggregation mechanism (INDICATORS_LAYER)

> **Decision: Option B — rollup worker via `feature_overlap`.**
> `feature_overlap` and `feature_overlap_type_pair` tables have been added (r009).
> The rollup worker should read `feature_overlap` instead of `feature.parent_id`,
> covering both hierarchical and non-hierarchical cases uniformly.
> This issue is resolved at the schema level; the rollup worker contract still
> needs to be added to `JOB_LAYER.md` once the first indicator requires it.

**Where:** `indicator_catalog.aggregation_method`, `indicator_result`, `JOB_LAYER.md`

**The gap:**
`aggregation_method` (`weighted_mean`, `sum`, etc.) declares the intent for rolling up indicator values from a finer spatial grain to a coarser one — for example, from block to AGEB, or from lot to block. But nothing in the framework defines who performs this rollup or when. The field is currently documentation intent, not a wired pipeline step.

**What needs a decision:**

| Option | How it works | Trade-off |
|---|---|---|
| **A — Model computes at each grain independently** | The worker receives features at each declared grain and computes the indicator directly at that scale | Simple worker contract; coarser results are not derived from finer ones — they are independently computed and may not be numerically consistent |
| **B — Spatial rollup worker** | After fine-grain results are written, a dedicated rollup worker walks `feature.parent_id`, groups child results by parent, applies `aggregation_method`, and writes a new `indicator_result` for the parent feature | Coarser results are always consistent with finer ones; requires defining a new worker type and queue in JOB_LAYER |

**Decision needed:** which option is adopted, and for which indicators?

If Option B: the rollup worker contract must be added to `JOB_LAYER.md` (queue name, trigger condition, input/output contract) and a use-case example must be added to `INDICATORS_LAYER.md`.

---

## 2. AOI-level aggregate indicator values (INDICATORS_LAYER + DATA_LAYER)

**Where:** `indicator_result`, `area_of_interest`, `aoi_feature`

**The gap:**
`area_of_interest` is not a `feature` — it lives in `curated_geo.area_of_interest`, not in `curated_geo.feature`. It therefore has no `feature_id` and cannot have an `indicator_result` row. There is no way to store a pre-computed single summary value such as "the liveability index of this AOI = 0.71".

The current AOI query in `INDICATORS_LAYER.md` returns per-feature results within the AOI, with `overlap_ratio` available as a display weight. The AOI-level aggregate must be recomputed at query time every time it is needed.

**What needs a decision:** does the platform need a stored, cacheable AOI-level summary, or is on-the-fly aggregation acceptable?

| Option | How it works | Trade-off |
|---|---|---|
| **A — Compute on-the-fly** | Add a canonical area-weighted aggregation query to `INDICATORS_LAYER.md` as the standard AOI summary pattern; no schema change | No schema change; consistent with current data at all times; slower at read time for large AOIs |
| **B — New `aoi_indicator_summary` table** | A new table keyed on `(aoi_id, scenario_id, indicator_id)` stores pre-computed AOI summaries; populated by a rollup step after fine-grain results are written | Cacheable and consistent with the rest of the result pattern; requires schema addition, a new worker step, and a staleness/refresh mechanism parallel to `indicator_result` |

**Decision needed:** is the frontend expected to display a single score-card value per AOI (requiring Option B), or only per-feature choropleth maps within an AOI (Option A is sufficient)?

---

## 3. Non-hierarchical spatial aggregation (INDICATORS_LAYER + DATA_LAYER)

> **Resolved.** `feature_overlap` (r009) covers both hierarchical and non-hierarchical
> cases.  The rollup worker in issue 1 above should use `feature_overlap` as its
> join source for all spatial aggregations.

**Where:** `indicator_catalog.aggregation_method`, `feature.parent_id`, `aoi_feature`

**The gap:**
Issue 1's Option B (rollup worker via `feature.parent_id`) only works when the two spatial grains are part of the same administrative hierarchy — for example, lots nested inside blocks, blocks nested inside AGEBs. When the source and target grains are **not hierarchical** — for example, blocks and a 100m analysis grid, lots and road segments, AGEBs and a custom project grid — `parent_id` is `NULL` and the hierarchy cannot be traversed. There is no structural relationship to follow, and the rollup worker has no way to know how much of a source feature falls inside a target feature.

The only pre-computed spatial intersection in the schema is `aoi_feature`, which handles AOI ↔ feature overlap. There is no equivalent for feature ↔ feature overlap across arbitrary type pairs.

**What needs a decision:**

| Option | How it works | Trade-off |
|---|---|---|
| **A — Model computes independently at each grain** | Each grain is treated as a fully independent computation; no relationship between results at different scales is assumed or enforced | Simple; always possible; results at different grains are not numerically consistent with each other |
| **B — `feature_overlap` pre-computed intersection table** | A new table generalises `aoi_feature` to feature-to-feature: keyed on `(source_feature_id, target_feature_id)`, storing `overlap_basis`, `overlap_measure`, `feature_measure`, `overlap_ratio` for any two feature types. A background job populates it for declared type-pair combinations. The rollup worker reads it instead of `parent_id`. | Consistent cross-grain aggregation for any type pair; requires schema addition, a background intersection job, and a refresh trigger when features change |

**Relationship to Issue 1:** Option B here is the generalisation of Issue 1's Option B. If a rollup worker is adopted, it should use `feature_overlap` (not `parent_id`) as its spatial join source — covering both hierarchical and non-hierarchical cases uniformly. `parent_id` would then become a special case of a known 1:1 containment overlap.

**Decision needed:** are non-hierarchical cross-grain aggregations required by any model family or by the frontend? If yes, `feature_overlap` should be designed alongside `aoi_feature`, and Issues 1 and 3 should be resolved together.

---
