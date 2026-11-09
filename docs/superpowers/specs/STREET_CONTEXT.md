# Spec: street context, from the network the platform already loads

Give the map a street layer without adding an external basemap, by
publishing the road network that is already in the database as features.

**Status:** spec. Written before the code.

**Scope:** one new module (`reimagina/data/roads.py`), two call sites (the
OSM loader's bootstrap and the synthetic territory generator), one frontend
layer, map attribution, and tests. **No migration**, no change to
`serving_geo`, no new tile function, no change to `get_tile`.

**Anti-scope:**

- **No external basemap.** Not a raster tile provider, not a vendored style,
  not a fallback. The whole point is that this needs none.
- **No change to the routing topology.** `curated_geo.road_edge` stays the
  source of truth for the graph; this reads it and writes nothing back.
- **No road attribution beyond what the graph carries.** The loader stores
  `osm_id`, endpoints and length. It does not store the way's name or
  highway class, so neither will appear. Inventing a classification from
  length would be fabrication.
- **No zoom-floor migration.** See "The risk I am not closing" below.

**Honest scale:** about 120 lines of projection, 40 of wiring, a frontend
layer, and tests. It adds no analytical capability. What it adds is the
ability to tell *where you are*, which every other feature in the map
silently assumes you already know.

---

## Why, and why this way

The map renders the territory in a void. There is no coastline, no street
grid, no label, no anything — a block is a rectangle floating in black. A
planner cannot tell which neighbourhood they are looking at, which way a
street runs, or whether the empty area to the east is a park or the edge of
the loaded data.

`main.ts` explains the absence, and the reason is good:

> Boots MapLibre with a minimal offline style (no basemap — the prototype
> avoids external tile dependencies and licensing drift)

Both halves of that matter for this delivery. An external tile provider is a
runtime dependency on someone else's service and a licence the Assignee did
not agree to, in a handover whose Article 4.1 turns on being able to state
exactly what the recipient is taking on.

**The answer needs no external anything, and it is already three-quarters
built.**

| | |
|---|---|
| `curated_geo.feature_type_catalog` | holds `global:road` — `geometry_family: line`, `role: infrastructure` — seeded by `data/feature.py` since the beginning |
| `serving_geo.roads` | exists as `feature_v WHERE type_slug = 'road'`, and returns **0 rows** |
| `curated_geo.road_edge` | holds the network: 40 edges here, an OSM walking graph in a real deployment |
| `serving_geo.get_tile` | serves `role = 'infrastructure'` from z12 and above, through the existing role gate |

Everything is in place except the step that connects the graph to the
feature table. Nothing anywhere writes a road feature.

**So this is a projection, not an ingest.** `publish_road_features` reads
`road_edge` and writes `feature`. One function, called by every producer of
a graph, rather than road-writing logic duplicated in the OSM loader and the
synthetic generator — and `get_tile?type_slug=road` then serves streets
through the path the baseline layers already use, with no new tile function
and no migration.

**The test for whether this is worth doing properly:** *if a user still
cannot tell which way is north and where the data ends, nothing was gained.*
A street layer that only draws at z14 and above, or that cannot be told
apart from the block outlines, would be decoration. It has to read as
context — beneath the data, visible at the zoom where the data is, and
obviously a street.

---

## Design

### The projection

```python
publish_road_features(conn, *, source_id, ingestion_run_id, dataset_version) -> int
```

- Reads `curated_geo.road_edge`; writes `curated_geo.feature` with
  `type_id = global:road`.
- `source_record_ref = f"edge:{id}"`, `attributes = {osm_id, length_m}`.
- **Replace, not upsert.** `load_graph` truncates and reloads the whole
  network, and `road_edge.id` is a sequence — so the same street gets a new
  id on every reload. Upserting on `source_record_ref` would leave the
  previous load's features behind as orphans, slowly doubling the street
  layer on each ingest. The publish deletes this source's road features
  first, which also matches the loader's own documented semantics
  ("loading replaces the whole network").
- `name` stays `NULL`. The graph does not carry the way's name, and a
  derived label would be invention.

**Why derive inside `curated_geo` at all.** `feature` is the canonical
spatial anchor, and most of it comes from an ingest. This comes from
another curated table — the same shape as `feature_overlap`, which is also
derived rather than loaded. The alternative is for `load_graph` to write
both, which couples the OSM loader to the feature table and leaves the
synthetic generator to reimplement it.

### Provenance, which closes a gap the loader already admits

`osm_network.py` says of itself:

> This function opens no `ingestion_run` and publishes no version, so
> reloading the graph leaves every accessibility indicator reporting a value
> computed against the *previous* network.

Writing features requires a source and a run — the `feature_before_write`
trigger enforces that `feature.source_id` matches
`ingestion_run.source_id` — so the publish step has to open one. That is not
incidental: it means the OSM network finally gets a row in
`data_source_catalog`, with its licence recorded in the column that exists
for it.

- name: `OpenStreetMap street network`
- provider: `openstreetmap`
- licence: **`ODbL-1.0`**

### Attribution, which is the licensing half

OSM is ODbL. Displaying it obliges attribution, and the delivery's whole
argument for avoiding an external basemap was licence hygiene — so adding
OSM-derived geometry without saying so would undo the reason for the
decision.

MapLibre's `AttributionControl` is already on the map (the "MapLibre" badge
bottom-right). The street source declares
`© OpenStreetMap contributors`, which MapLibre shows **only while that
source is in the style** — so a deployment with no street data shows no
claim, and one with streets cannot forget to.

### The frontend layer

A line layer in `INPUT_LAYERS` beside Buildings / Blocks / Municipalities,
through the existing `get_tile?type_slug=road` path. Styled as context and
not as data: thin, low-contrast, under every analysis layer, widening
slightly with zoom. `minzoom: 12`, matching the server's role gate exactly —
a client minzoom below what the server serves produces empty tiles that look
like missing data.

Default **on**, unlike every other optional layer here, because orientation
is the problem it exists to solve and a context layer nobody switches on
provides none.

---

## The risk I am not closing

At z12 a tile covers roughly 10 km. The building layer was measured at that
zoom and found to be **2.59 MB per tile** — which is why `r038` floors
buildings at z14.

Roads will be lighter than buildings (fewer features, and `ST_Simplify` at
2 m applies from z12) but this is a *walking* network, which is denser than
a road centreline network, and I have 40 synthetic edges to measure rather
than a city. **I am not going to claim a number I cannot produce.**

So: ship on the existing role gate, and record that if a real deployment
finds z12 road tiles too heavy, the remedy is the one `r038` already
demonstrates — a `type_slug` zoom floor in `get_tile`, chosen from a
measurement rather than from a guess. The measurement procedure is in
`r038`'s docstring.

What I *can* and will measure here: the tile size the synthetic network
produces, and that the layer round-trips through the real tile path.

---

## Tests

| Layer | What |
|---|---|
| `tests/integration` | the projection writes one feature per edge with the right type and geometry family; a second publish replaces rather than accumulates; `serving_geo.roads` becomes non-empty; `get_tile?type_slug=road` returns decodable road geometry at z12 and nothing below it |
| `tests/unit` | the frontend layer's minzoom matches the server's role gate — the pair that produces invisible-but-present tiles when it drifts |
| `tests/e2e` | the map requests street tiles and the attribution names OpenStreetMap |

## Maintenance checklist

1. No migration — the type, the view, the tile function and the role gate
   all exist.
2. `serving_geo` unchanged. `serving_geo.roads` starts returning rows, which
   is the view doing what it was written for.
3. `explore` unchanged; `feature_type_stats_v` picks up the new type_slug
   on its own, which is the inspection surface improving for free.
4. CLI unaffected.
5. Tests added.
6. `AGENTS.md`, `TILE_SERVING.md`, `infra/tiles/layers.json` and the
   delivery documentation updated.

---

## What the implementation found

### 1. Everything was already there, and that was the finding

The spec guessed that the type, the view and the role gate existed. They
all did, exactly as written, and the role gate turned out to serve
`infrastructure` from z12 with no change needed — so the whole feature is
**a projection function, two call sites and a layer spec**, with no
migration at all. That is unusual enough to be worth stating: the schema
was designed for this and then nobody wrote the connecting step.

### 2. A publish that runs after the AOI membership pass is invisible

The first run wrote 40 road features, `serving_geo.roads` reported all 40
— and the map drew no streets. Every street tile came back
`204 No Content`.

`get_tile` scopes by AOI through an `EXISTS` against
`curated_geo.aoi_feature`, and the synthetic generator computed membership
*before* the graph was loaded. The roads were in `curated_geo.feature`, in
`serving_geo.roads`, correctly typed, correctly scoped by zoom — and in no
AOI, so the one filter the frontend always sends removed them.

Nothing failed. There is no error anywhere in that path, and the symptom
is identical to "the layer is broken". The membership pass now runs last,
after every feature exists, with the reason recorded at the call site.

### 3. `ingestion_run` is UNIQUE on the run key

A fixed `orchestrator_run_id` of `roads-{version}` made the *second*
publish of the same version fail with a constraint violation — which is
re-running the loader after a network reload, the exact thing the function
exists to support. The id is timestamped now. Accumulating one run per
publish is correct rather than a leak: each publish is a distinct ingestion
event, and a table of them is what provenance means.

### 4. Attribution could not live in the frontend

The spec said to put `© OpenStreetMap contributors` on the street source.
That would print it on every synthetic territory — a false provenance claim
in a product whose argument is that its provenance is checkable.

So the credit is derived: `GET /tile-sources/attribution` reads
`data_source_catalog.license` for sources that actually have features, and
the map renders what it gets. Verified both ways — the synthetic territory
credits `reimagina-synthetic` and makes no OSM claim, and
`attribution_text("openstreetmap", "ODbL-1.0")` returns the wording the
licence prescribes. Registered-but-unloaded sources are excluded, because
crediting one would say the map shows data it does not.

This is also what pushed the OSM source row into existence with its licence
recorded, which closes half of a gap `osm_network.py` already admits to:
`load_graph` "opens no `ingestion_run` and publishes no version".
`feature_before_write` rejects a feature whose source disagrees with its
run, so the publish cannot skip one.

---

## Measured

Tile sizes for the street layer on the synthetic territory (40 edges,
~5.2 km), over the tile containing it:

| z | road tile | candidates |
|---|---|---|
| 11 | **0 B** | — (below the role gate) |
| 12 | 2,714 B | 40 |
| 13 | 2,714 B | 40 |
| 14 | 1,864 B | 27 |
| 16 | 1,670 B | 24 |

About 68 bytes per edge at z12. **This is 40 edges, not a city**, and the
spec's open risk stands: extrapolating linearly is exactly the mistake
`r038` documents making about buildings, where the cheap-looking low zooms
turned out to be cheap because simplification was discarding 95 % of the
data. The number here is what the layer costs on the fixture, and nothing
more.

## Verified

- `tests/integration/test_road_features.py` — 17 tests: the projection, the
  trigger accepting line geometry, replace-not-accumulate (confirmed failing
  against an upsert), source scoping, `serving_geo.roads` filling, the z12
  role gate, a decoded road tile, licence recording, and the two
  false-attribution cases.
- `apps/web` — the client floor pinned to the server's role gate, draw
  order, line geometry, default visibility.
- `tests/e2e` — street tiles returning linework in a real browser, the
  toggle present and first, and the map crediting its data.
