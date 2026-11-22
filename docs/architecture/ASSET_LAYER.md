# Asset Layer

**Related documents:** `INDICATORS_LAYER.md` — the outputs whose dependencies
this graph records. `JOB_LAYER.md` — the runs it decides the contents of.
`DATA_LAYER.md` — the sources that sit at its leaves.

Introduced by `r041`; `r044` pins the version hash to byte order and `r045`
retires the single `scenario.override` asset in favour of one per attribute. The
layer answers one question the rest of the platform could not: **what is
actually out of date, right now.**

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
`urban_form_block_building_density` has `reads=("geometry",)` while its
compute joins `global:building`; the accessibility family reads
`poi_version` / `network_version`, hand-maintained tokens standing in for whole
tables. An asset edge names the real producer: each `IndicatorSpec` declares its
`inputs` as `AssetEdge`s, and `reads` / `depends_on` are projections of them.

## The shape

```
asset  ──edge──▶  asset            declared, in code
  │
  ├── version      derived from the graph, or recorded at a leaf
  └── materialization   what a compute actually produced
```

| table | holds |
|---|---|
| `curated_geo.asset` | the node — `kind` `source`, `external` or `derived`; one per source layer, external layer, per-attribute scenario override, or indicator |
| `curated_geo.asset_dependency` | the edge: `relation`, an `attributes` projection, `radius_m` for `within_radius`, an optional `weight` for `derived_from`, and `partition_mapping` |
| `curated_geo.asset_version` | recorded versions for **leaves**, Iceberg-snapshot style: append a row, move `is_current` |
| `curated_geo.asset_materialization` | what a run actually wrote, per partition, with the input versions it read and whether it covered the whole grain |

| view | answers |
|---|---|
| `serving_geo.asset_expected_version_v` | what each asset's version *should* be, derived from the graph |
| `serving_geo.asset_freshness_v` | expected vs recorded → `fresh` / `stale` / `partial` / `absent` |
| `serving_geo.asset_component_v` | the terms hashed into each version — one row per node and edge in the asset's upstream closure, depth-capped at 50 |
| `serving_geo.asset_lineage_v` | upstream / downstream reachability |
| `explore.asset_v`, `explore.asset_freshness_v`, `explore.asset_lineage_v` | the engineer-facing mirrors |

The shipped registry (`assets/registry.py`) holds 30 nodes:

| asset keys | kind | backing |
|---|---|---|
| `mex.state`, `mex.municipality`, `mex.neighborhood`, `mex.ageb`, `mex.block`, `mex.lot`, `global.building`, `global.poi` | source, `commit` | a feature type (`global.poi` is `global:establishment`) |
| `censo.2020` | source, `commit` | the `censo_2020` observation prefix |
| `global.road_network` | external, `observed` | the relation `curated_geo.road_edge` |
| `scenario.override.<attribute>` — `floors`, `geometry`, `lot_coverage`, `network_version`, `num_levels`, `people_per_household`, `poi_version`, `unit_size` | source, `commit`, partitioned by `scenario` | one per attribute any indicator reads |
| `indicator.<slug>` — the 12 shipped indicators | derived, partitioned by `scenario` | projected from `SHIPPED_INDICATORS` |

Edges are the indicators' declared `inputs` plus, for every attribute an
indicator reads, a `self` edge to that attribute's override asset — 48 in all.
`assets seed` (and `bootstrap-db`) writes both, replacing each indicator's edges
rather than merging them.

## Four decisions worth knowing

**Partitions, not whole assets.** An asset is materialised per *partition*.
`assets/keys.py` is the one canonicalisation of a partition key — dimensions
sorted by name, rendered `name=value`, joined by `/`, and the baseline is the
empty string — so the SQL view and the Python twin cannot disagree about what a
partition is called. The shipped registry partitions by `scenario` only (the
indicators and the override assets); a producer without that dimension is read
at the baseline (`partition_mapping='baseline'`), one with it at the consumer's
own scenario (`'identity'`), and `asset_component_v` carries a scenario
partition downstream along `identity` edges.

**The version algebra exists twice, deliberately.**
`serving_geo.asset_expected_version_v` is what the platform reads: always live,
`O(graph)` rather than `O(rows)`, no job to keep it true. `assets/version.py`
derives the same value in Python, from the same rows, by a different route —
purely so `tests/integration/test_asset_graph.py` can assert the two agree.
Two implementations of one algebra is an uncomfortable choice made on purpose;
the alternative is a view nothing can check. Both hash the closure as a set,
sorted in byte order (`COLLATE "C"`, r044), with floats at a fixed scale of 6.

**Leaves are told; everything else is derived.** A source asset's version is
recorded by whatever changed it (`assets/publish.py`): an ingest publishes its
ingestion run id when it finishes having written rows (`data.catalog.finish_run`),
a loader that writes block observations (the census, `overture block-storeys`)
publishes `mex.block`, the asset indicators declare those attributes on,
`assets backfill-versions` covers layers loaded before the graph existed, and
materialising a scenario publishes a content digest per edited attribute into
that scenario's partition (`scenarios.overrides.publish_scenario_version`). The
database can compute every *derived* version from the graph, but it cannot know
that Overture published a new release. `assets/observe.py` (`assets observe`) is
for data that arrives through no pipeline at all: it runs each `observed`
asset's `probe_sql` and publishes the result when it changes. One shipped asset
is observed — `global.road_network`, because `curated_geo.road_edge` is loaded
by `network bootstrap` (an Overpass fetch, or the source bundle's saved graph
with `--graph-dir`) with no `ingestion_run`; its probe is the row count and
the max `osm_id`. A swapped local copy of the INEGI parquet (the source
bundle's SHA256SUMS guards the bundle itself, not the database) and
`scripts/seed_demo_attributes.py`'s direct merges into `feature.attributes`
have no observed asset, so the graph cannot see them.

⚠ **A staleness system that reports "fresh" for data it cannot see is worse
than none**, because it is believed. That is why a probe that finds nothing
retires the asset's version (`retire_version`) rather than publishing a
sentinel, and why a partition with nothing recorded reports `absent`, never
`fresh`. `absent` is still excluded from the baseline dirty set by default: on a
fresh database every indicator is absent, and an empty submit would otherwise
recompute the whole platform. A scenario partition counts it.

**The registry is seeded from code, never from migration bodies.** `r006`,
`r015`, `r016`, `r017`, `r019`, `r020`, `r021` and `r041` itself each insert
their own `explore.layer_catalog` rows, and that drifted far enough to need
`scripts/repair_layer_catalog.sql`. One seeder cannot drift from itself.

## How it reaches a run

`assets/freshness.py` derives the dirty set from freshness rather than from
declared attributes. Both paths still exist and answer different questions;
`orchestration/plan.plan_run` picks between them:

```
attribute path   "an edit named X"      → impact.compute_dirty_set, IndicatorSpec.reads
freshness path   "what is out of date"  → assets/freshness.stale_units, the graph
```

A submit that names attributes or features takes the attribute path — the
scenario recompute does, with the scenario's footprint. A submit that names
neither takes the freshness path when the graph is seeded: on the baseline
partition it returns indicators in state `stale` or `partial`; with a
`scenario_id` it returns every indicator that has a node in that scenario's
partition and whose materialised version is missing, different or partial.
With an `aoi_id`, indicators already done for that AOI are left out (below).
Freshness units are whole-grain. A database with no seeded graph falls back to
the attribute path. `POST /jobs` also accepts `changed_datasets`, which
`impact.indicators_for_datasets` resolves from the `inputs` edges into an
explicit indicator list.

A road reload is seen when `assets observe` probes `curated_geo.road_edge` and
publishes a new version for `global.road_network`; every consumer's expected
version moves; the four `*_network_m` indicators and `livability_block_score` —
which consumes three of them — all report stale. **That propagation needs no
code**: the dependency is declared in the catalog and the graph finds it
(`tests/integration/test_freshness_driven_recompute.py`).

Materialisations are written by the runner registry, not by each compute:
`orchestration/runners._recording` wraps every dispatched unit in
`assets.materialize.materializing`, which captures the expected version and
input versions **before** the compute runs, records them only if it succeeds,
and marks coverage complete only when the unit was neither feature- nor
AOI-scoped, so a scoped run at the current version reports `partial`, not
`fresh`, in `asset_freshness_v` — the whole-database view.

Coverage is also recorded per AOI. A unit scoped to AOI X with no feature list,
or a scenario unit whose features include every feature the scenario overrides
(the recompute footprint: a block without an override reads the baseline through
the overlay), records `coverage_scope = {"complete": false, "aoi_id": X}`.
`assets.freshness.fresh_indicator_slugs` counts an indicator as done for X when
a succeeded materialisation at the expected version covered the database or X,
and no materialisation at another version came after it (override versions are
content digests and can return to an older value). Planning with an `aoi_id`
subtracts those: the freshness path on either partition, and an explicit
scenario change-set too, so an identical scenario recompute is a noop. A
feature-scoped baseline unit, or a scenario unit missing an overridden feature,
stays plain partial; another AOI and the whole database still see `partial`.

## What a params change does

`seed_assets` folds seven fields of each `IndicatorSpec` — `computation_kind`,
`value_type`, `unit`, `aggregation_method`, `valid_min`, `valid_max`,
`applies_to` — into the indicator asset's `params_digest`, which feeds the asset
version; a weight moves it through the edge term instead, since
`asset_component_v` hashes every edge's weight. So editing an indicator's bounds
or weights in `indicators/catalog.py` and re-seeding moves its expected version
and restages its results and everything downstream of them. That is intended —
a bounds change that alters the output *should* restage — but it means a catalog
edit with no compute-code change can dirty a whole grain: an edit like `#55`'s,
which moved the composite's `valid_max` from 300 to 1, restages the composite on
every block.

The digest is computed from the in-code spec at seed time, not read from the
`indicator_catalog` row, so an SQL edit to that table moves nothing.
`higher_is_better`, `cap_value`, `name` and `description` are not in it. A
change to an indicator's own logic is signalled by its per-asset
`code_version` — `"1"` for every shipped indicator, bumped by hand, never the git
SHA.
