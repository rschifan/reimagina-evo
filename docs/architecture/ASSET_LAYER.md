# Asset Layer

**Related documents:** `INDICATORS_LAYER.md` — the outputs whose dependencies
this graph records. `JOB_LAYER.md` — the runs it decides the contents of.
`DATA_LAYER.md` — the sources that sit at its leaves.

Introduced by `r041`. The layer answers one question the rest of the platform
could not: **what is actually out of date, right now.**

## Why it exists

The impact engine answers *"an edit named `floors`, who reads `floors`"* — a
projection of `IndicatorSpec.reads` onto the dirty set. That is a real answer
and it stays. But it only sees what an edit **names**, and three things go stale
without anyone naming them:

- a layer was **reloaded** — the Overture release moved, the road network was
  re-fetched — and no attribute changed
- an indicator's **own logic** moved: a bounds change, a weight change, a new
  formula
- a **crash** left a run half-finished, so some results exist and some do not

None of those produce an attribute name, so none of them dirty anything under
the attribute path. Freshness is derived from *versions* instead, which is a
different and larger question.

⚠ It also closes a gap `reads` structurally cannot: `reads` is an
**invalidation vocabulary, not a data manifest** (see `INDICATORS_LAYER.md`).
`urban_form_block_building_density` declares `reads=("geometry",)` while its
compute joins `global:building`; the accessibility family declares
`poi_version` / `network_version`, hand-maintained tokens standing in for whole
tables. An asset edge names the real producer.

## The shape

```
asset  ──edge──▶  asset            declared, in code
  │
  ├── version      derived from the graph, or recorded at a leaf
  └── materialization   what a compute actually produced
```

| table | holds |
|---|---|
| `curated_geo.asset` | the node — one per source, indicator, or external layer |
| `curated_geo.asset_dependency` | the edge, with `partition_mapping` and an optional `weight` |
| `curated_geo.asset_version` | recorded versions for **leaves**, Iceberg-snapshot style: append a row, move `is_current` |
| `curated_geo.asset_materialization` | what a run actually wrote, per partition |

| view | answers |
|---|---|
| `serving_geo.asset_expected_version_v` | what each asset's version *should* be, derived from the graph |
| `serving_geo.asset_freshness_v` | expected vs recorded → `fresh` / `stale` / `absent` |
| `serving_geo.asset_component_v` | the transitive closure, depth-capped at 50 |
| `serving_geo.asset_lineage_v` | upstream / downstream reachability |
| `explore.asset_v`, `explore.asset_freshness_v`, `explore.asset_lineage_v` | the engineer-facing mirrors |

## Four decisions worth knowing

**Partitions, not whole assets.** An asset is materialised per *partition* — a
scenario, an AOI, a feature-type pair, a zoom tier. `assets/keys.py` is the one
canonicalisation of a partition key, so the SQL view and the Python twin cannot
disagree about what a partition is called.

**The version algebra exists twice, deliberately.**
`serving_geo.asset_expected_version_v` is what the platform reads: always live,
`O(graph)` rather than `O(rows)`, no job to keep it true. `assets/version.py`
derives the same value in Python, from the same rows, by a different route —
purely so `tests/integration/test_asset_graph.py` can assert the two agree.
Two implementations of one algebra is an uncomfortable choice made on purpose;
the alternative is a view nothing can check.

**Leaves are told; everything else is derived.** A source asset's version is
recorded by whatever changed it (`assets/publish.py`). The database can compute
every *derived* version from the graph, but it cannot know that Overture
published a new release. `assets/observe.py` covers data that arrives through no
pipeline at all — `curated_geo.road_edge` comes straight from Overpass with no
`ingestion_run`, the Azure mirrors are copied by hand, and
`scripts/seed_demo_attributes.py` merges directly into `feature.attributes`.

⚠ **A staleness system that reports "fresh" for data it cannot see is worse
than none**, because it is believed. That is why `absent` is excluded from the
dirty set by default rather than treated as fresh.

**The registry is seeded from code, never from migration bodies.** `r006`,
`r015`, `r016`, `r019` and `r020` each insert their own `explore.layer_catalog`
rows, and that drifted far enough to need `scripts/repair_layer_catalog.sql`.
One seeder cannot drift from itself.

## How it reaches a run

`assets/freshness.py` derives the dirty set from freshness rather than from
declared attributes. Both paths still exist and answer different questions:

```
attribute path   "an edit named X"      → impact.py, IndicatorSpec.reads
freshness path   "what is out of date"  → assets/freshness.py, the graph
```

A road reload publishes a new version for `global.road_network`; every
consumer's expected version moves; the three `*_network_m` indicators and
`livability_block_score` — which consumes them — all report stale. **That
propagation needs no code**: the dependency is declared in the catalog and the
graph finds it (`tests/integration/test_freshness_driven_recompute.py`).

## What a params change does

Catalog fields fold into a `params_digest` that feeds the asset version, so
editing an indicator's bounds or weights moves its expected version and
restages its results. That is intended — a bounds change that alters the output
*should* restage — but it means a catalog edit with no code change can dirty a
whole grain. `#55` moving the composite's `valid_max` from 300 to 1 did exactly
that.
