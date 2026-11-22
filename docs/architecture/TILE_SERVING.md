# Tile Serving

Tile serving follows a hybrid rule:

- **live MVT** is the system of record for current, filtered, scenario-aware, or frequently changing map data
- **PMTiles** are snapshot artifacts for stable reference/context layers

## Current v1 contract

- `serving_geo.get_tile()` is the primary live tile path, served by
  **Martin** by default and by **the API itself** as an alternative — see
  "Two servers, one contract" below
- `serving_geo.indicator_overlay_tile()` adds a live choropleth channel for
  indicator results, and `serving_geo.aggregation_overlay_tile()` one for
  current observations (details below), both published by both servers
- PMTiles are additive, not a replacement
- v1 PMTiles target: `admin_reference` from `serving_geo.admin_v`
- PMTiles are rebuilt by running `tiles export` (see "CLI" below); nothing
  in the repository schedules a rebuild or triggers one on a release
- PMTiles are **not** rebuilt on every ingestion run

## Two servers, one contract

The contract is the **function signature**, not the server. Every tile
function takes `(z integer, x integer, y integer, query_params json)` and
returns `bytea`; Martin's function-source convention exposes exactly that at
`/{function}/{z}/{x}/{y}` with the query string forwarded as `query_params`.
Because the signature is the contract, there can be more than one thing
serving it.

| | Serves | When to use it |
|---|---|---|
| **Martin** (`:3000`) | `ghcr.io/maplibre/martin` against Postgres directly | The default. A Rust tile server with its own pool is the better machine under many clients panning at once |
| **The API** (`:8001/tiles`) | `apps/api/routers/mvt.py`, same functions, same process as the rest of the API | One less service to run; the only path that is end-to-end covered by this repository's own test suites |

Choosing between them is one environment variable, read by
`tiles.defaults.tile_live_base_url` and resolved by `tiles.sources`:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000         # Martin
REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles                    # the API, same-origin
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:8001/tiles   # the API, direct
```

Unset, the code default is `http://localhost:3000/get_tile` — Martin, which
resolves to the same URLs as the first line. `make dev` is the exception: it
starts the API, the web dev server and the worker but not Martin, so it sets
`/api/tiles` (as does `make dev-api`, which runs the same preamble) unless the invoking shell already sets the variable (a value in
`.env` does not count; the Makefile restores the shell's value after sourcing
it).

⚠ **Prefer the relative form.** An absolute URL makes every tile request
cross-origin from the web dev server, so it depends on the API's CORS
allowlist — which defaults to `http://localhost:5173` and
`http://localhost:8001` (`REIMAGINA_API_CORS_ORIGINS` overrides it) and
therefore rejects a browser that reached the page as
`http://127.0.0.1:5173`. Measured in a real
browser: 53 tile requests, 53 `net::ERR_FAILED`, a map drawing nothing, and
**no console error naming CORS**. `/api/tiles` goes through the proxy the web
app already uses for the rest of the API, so tiles are same-origin and the
allowlist does not enter into it. Verified from an origin deliberately absent
from the allowlist: 38 tile responses, 200s and 204s, map drawn.

Nothing else changes — not `infra/tiles/layers.json`, not the tile-source
resolver, not the frontend. `_martin_base_and_default_function` treats a
trailing path segment as a function name only when it is one of the known
functions, so `/tiles` is kept as a path prefix rather than mistaken for a
function called "tiles". A contract test pins that, because "one variable" is
the property the second implementation has to keep.

**The servable functions are one list**, `reimagina.tiles.functions.TILE_FUNCTIONS`
— `get_tile`, `indicator_overlay_tile`, `aggregation_overlay_tile` —
read by the resolver and by the API route's allowlist. A function the
resolver advertises but the route refuses is a 404 in a map; a function the
route serves but the resolver never names is dead code.

### Why both, rather than picking one

**Martin is the better production machine** and nothing here claims
otherwise. Measured on the synthetic territory, `get_tile` answers a
populated z16 tile in ~1.5 ms, so neither Postgres nor FastAPI is the
constraint at that scale — but at city scale with many concurrent clients, a
dedicated tile server is the right answer.

**The API route is what makes the platform complete without it.** Martin is
an external binary, so until this existed the live tile path was proven at
the SQL layer (26 integration tests over the three functions) and at the
decoder layer (`scripts/mvt_inspect.py`) with an unexecuted process in
between. It also makes the vector path consistent with the raster path, which
has always been served in-process: `apps/api/app.py` mounts titiler's COG
endpoints under `/cog` so "the API + tile server share a process, a dep tree,
and an origin".

**And it closes a bug class.** `/health/tile-server` exists only because the
API and Martin read their database from two different variables — the API
`REIMAGINA_APP_DSN`, Martin its own `DATABASE_URL` (built by the compose
file from `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` over the
compose network, `postgres:5432`) — and can disagree about which
database they serve; its own comment records that the multi-site work
shipped twice against mismatched databases. It probes Martin at
`REIMAGINA_MARTIN_URL` (default `http://localhost:3000`) and reports the
API's database name for an operator to compare; it cannot read Martin's. A
deployment without Martin cannot have that bug.

### What the API route adds on top of the SQL

The bytes are one function call; the HTTP is the work.

| Condition | Response |
|---|---|
| Tile has features | `200`, `application/vnd.mapbox-vector-tile` |
| Tile is empty | `204 No Content` — matching Martin, and matching what this document already specified for sub-`z14` buildings |
| `If-None-Match` matches | `304 Not Modified` |
| `z` outside 0–24, or `x`/`y` outside `0 … 2^z − 1` | `422`, before the database is touched |
| Unknown function | `404`, naming the servable ones |
| Every pooled connection still busy after `REIMAGINA_API_POOL_WAIT_S` (default 30 s) | `503` with `Retry-After: 1` |

- **`ETag`** is a content hash (SHA-256, truncated to 128 bits) with the
  encoding folded in, plus `Vary: Accept-Encoding`. A shared cache must not
  be able to serve a gzip body under an identity validator. The `304` is
  decided after the tile is computed: it saves the transfer, not the query.
- **`Cache-Control`** `public, max-age=60, stale-while-revalidate=300`
  (`REIMAGINA_TILE_MAX_AGE_S`, `REIMAGINA_TILE_STALE_WHILE_REVALIDATE_S`), on
  `204`s as well as `200`s, for every tile **except a scenario's** (any
  request carrying `scenario_id`, delta tiles included), which is `no-cache`.
  A scenario changes under the same URL
  each time it is recomputed, and the map rebuilds its source with that URL
  when the run lands; with a lifetime the browser painted the previous run's
  tiles for up to six minutes while the table already showed the new totals.
  `no-cache` still stores the tile, and the ETag keeps an unchanged one at a
  `304`.
- **gzip** above 1 KiB only. Measured: 1.86x on a real tile — MVT is already
  a packed protobuf, so a `304` is worth far more than compression, which is
  why conditional requests are in scope and a tile cache is not.
- **The read is scoped to a savepoint.** `SET LOCAL transaction_read_only`
  is transaction-scoped, so setting it on the ambient transaction and rolling
  back was fine for a request that owns its connection and destructive for
  anything sharing one. Postgres stacks GUC settings per subtransaction, so
  `ROLLBACK TO SAVEPOINT` releases the setting and leaves an outer
  transaction untouched.
- **The read is time-boxed.** `SET LOCAL statement_timeout` inside the same
  savepoint, from `REIMAGINA_TILE_TIMEOUT_MS` (default 5000), so a
  pathological tile fails its own request instead of holding a pooled
  connection.
- **A burst waits for a connection rather than failing.** The route takes a
  connection from the API pool (`REIMAGINA_API_POOL_MAX`, default 16).
  psycopg2's pool raises past its size instead of blocking, which a map
  loading a new zoom hits at once — measured, 40 concurrent tile requests
  came back 16 × `200` and 24 × `500` on an idle server — so `get_db_conn`
  takes a semaphore permit first and the excess queues for up to
  `REIMAGINA_API_POOL_WAIT_S` before the `503` above.

`GET /tiles/{function}` returns TileJSON 3.0.0 (`minzoom` 0, `maxzoom` 22,
tile URL built from the incoming request), so the route is drop-in for the
resolver's `tilejson_url` as well as its `tiles_url_template`.

## Street context, without an external basemap

The map has no basemap, deliberately — `main.ts`: "the prototype avoids
external tile dependencies and licensing drift". Spatial reference comes
instead from the platform's own street network.

`curated_geo.road_edge` holds the routing graph the accessibility
indicators walk. `reimagina.data.roads.publish_road_features` projects it
into `curated_geo.feature` as `global:road`, and from there everything
already existed:

- `global:road` is in `feature_type_catalog` (`geometry_family: line`,
  `role: infrastructure`) and has been since the first seed
- `serving_geo.roads` is `feature_v WHERE type_slug = 'road'`
- `get_tile?type_slug=road` serves them through the same path as every
  other baseline layer

**No migration, no new tile function.** The only thing that was missing was
the step connecting the graph to the feature table.

**Roads appear from z12**, and not by a client setting: they carry
`role = 'infrastructure'`, which the role gate below serves only from z12.
Measured on the running database — a z11 road tile is 0 bytes, a z12 one is
2,714. The frontend's `minzoom: 12` matches, and a unit test pins the pair,
because a lower client floor only buys requests that come back empty and on
a map empty is indistinguishable from missing.

⚠ **The density risk is open, not closed.** At z12 a tile covers ~10 km, and
that is the zoom where the *building* layer measured 2.59 MB per tile (which
is why `r038` floors buildings at z14). A walking network is denser than a
road centreline network. This ships on the role gate with 40 synthetic edges
measured, not a city — if a real deployment finds z12 road tiles too heavy,
the remedy is the one `r038` demonstrates: a `type_slug` zoom floor in
`get_tile`, chosen from a measurement. The procedure is in `r038`'s
docstring.

**Attribution is derived, not declared.** `GET /tile-sources/attribution`
returns a credit line per source that actually has features, built from
`data_source_catalog.license`. OpenStreetMap gets the wording ODbL
prescribes; a synthetic territory credits the synthetic source and makes no
OSM claim. Hardcoding the credit in the frontend would be a false provenance
statement on every demo.

### `serving_geo.get_tile(z, x, y, query_params json)`

The baseline feature channel. Martin publishes it at
`GET /get_tile/{z}/{x}/{y}?…`; the API publishes the same at
`GET /tiles/get_tile/{z}/{x}/{y}?…`. Query params: `type_slug`, `role`, `aoi_id`
— all optional. One MVT layer, `features`, with `id`, `type_slug`, `role`,
`name` and `attributes` per feature.

- **The role gate.** Below z8 only `role = 'context'` is served; z8–z11 adds
  `analysis_unit`; from z12 every role, which is where `infrastructure`
  (roads) appears.
- **Attributes thin out with zoom.** From z14 `attributes` is the feature's
  own merged with its current observations
  (`serving_geo.feature_current_attributes_mv`); at z12–13 the feature's own
  only; below z12 none. Geometry is simplified below z14 (tolerance 0.00002
  at z12–13, 0.0001 at z10–11, 0.0005 at z8–9, 0.002 below z8).
- **`aoi_id` scopes to that AOI's members** via `EXISTS` against
  `curated_geo.aoi_feature` (r039). `EXISTS` rather than a join, because a
  feature can belong to several AOIs and a join would emit it once per
  match and duplicate it in the tile.
  - Omitting it stays **unscoped** — a CDMX-wide view is a legitimate ask,
    and the parameter narrows rather than enables.
  - `?aoi_id=` (present but blank) reads as unscoped; an empty string is
    how a client serialises "no AOI selected".
  - A malformed uuid renders an **empty tile, not an error**. A tile
    function returns `bytea` and has no error channel, and empty is the
    fail-closed direction — before r039 an unrecognised param was silently
    dropped and showed *everything*.
- **Buildings are served only from `z >= 14`** (r038). Below that the
  request returns 204. This is not a preference:

  | zoom | candidates in tile | MVT bytes |
  |---|---|---|
  | 11 | 242,317 | 1.31 MB |
  | 12 | 35,033 | **2.59 MB** |
  | 14 | 421 | 31 KB |

  Against a renderer budget of ~500 KB with several tiles in flight, the
  layer never finished painting — presenting as *missing data* rather than
  as slowness. Two cheaper fixes were measured and rejected: dropping
  attributes saved 362 bytes of 2.59 MB (`ST_AsMVT` dictionary-encodes
  properties; the 1.71 MB hog is the per-feature uuid `id`), and
  simplifying harder kept only 1,826 of 37,858 footprints. At z12 one pixel
  is ~36 m and a building is ~10 m, so there is no encoding that helps.
  **For building information at AOI scale use the
  `urban_form_block_building_density` choropleth**, which is the layer that
  question actually wants.

  ⚠ The gate keys on `type_slug`, not `role`: buildings are
  `role='context'`, the same role as `municipality`, so any role-based rule
  that drops buildings at overview zoom also drops the municipality
  outlines.

### `serving_geo.indicator_overlay_tile(z, x, y, query_params json)`

- Martin auto-discovery publishes the function at
  `GET /indicator_overlay_tile/{z}/{x}/{y}?…`; the API at
  `GET /tiles/indicator_overlay_tile/{z}/{x}/{y}?…`. One MVT layer,
  `indicator_overlay`.
- Required query param: `indicator_slug` — the function returns an
  empty tile when absent so a misconfigured caller never leaks the
  whole table.
- Optional query param: `scenario_id` (uuid). Omit for base state. With it,
  each feature carries the scenario's value where the scenario has one and
  the baseline's otherwise (r043); since r049 the anti-join tests for a
  value on both arms, so a valueless scenario row no longer draws a feature
  twice.
- Optional query param: `feature_type` (r018) — one grain only. Without it
  every grain the indicator has results at is drawn; every shipped indicator
  is block-only, so in practice it changes nothing.
- Optional query param: `aoi_id` (r022; a malformed uuid is an empty tile
  since r024) — scoped by `EXISTS` against `curated_geo.aoi_feature`, as in
  `get_tile`.
- Optional query param: `delta` (r049) — a boolean in Postgres's own
  spelling, which is the *whole* vocabulary: `1`, `t`, `tr`, `tru`,
  `true`, `y`, `ye`, `yes`, `on`, case ignored, surrounding whitespace
  trimmed (`o` is excluded because Postgres excludes it — ambiguous
  between `on` and `off`). Anything else, `false` included, is the value
  view, so an unrecognised input falls back to the older behaviour rather
  than to the newer one. The prefixes and the trim are deliberate: a
  hand-typed `?delta=true ` would otherwise serve values to a client
  asking for a difference, silently.
- Returns empty below `z=8` — r025 lowered the floor from 10 once AOI
  filtering made a z=8 tile of one municipio ~19 k blocks rather than
  country-scale volume, and added the `ST_Simplify(…, 0.0005)` tier for
  z=8/9. `infra/tiles/layers.json` advertises `minzoom: 11` to clients,
  which is a rendering choice about where a choropleth reads well, not
  the function's floor; the two numbers are different claims and neither
  is 10. The functions' `COMMENT`s said z=10 until r052 corrected them
  to z=8 (comments only).
- Emits per feature: `feature_id`, `type_slug`, `value_numeric`,
  `quality_flag`. Frontend's `fill-color` expression reads
  `value_numeric`; `quality_flag` is there for a client that wants to dim
  `estimated` / `missing` rows — the web app does not read it today.
- **Two views, one function.** With `delta` set the tile emits
  `feature_id`, `type_slug`, `delta` and `state` — and *not*
  `value_numeric`, so a client that forgot to switch from the sequential
  value ramp to the diverging delta ramp paints nothing rather than
  painting a difference as a value. `state` is `changed`, `unchanged` or
  `new`, exactly as `GET /scenarios/{id}/comparison` reports it
  (`reimagina.scenarios.comparison`); the two surfaces are the same
  classification, and
  `tests/integration/test_indicator_overlay_delta.py::test_the_delta_tile_and_the_comparison_endpoint_agree`
  is what keeps them from drifting. A `new` feature carries no `delta` at
  all (`ST_AsMVT` omits a NULL property) because there is nothing to
  subtract from. `delta` without a valid `scenario_id` is an empty tile:
  serving baseline values under that property would paint every block
  `unchanged`, which is a finding produced by a missing parameter.
- The signature did not change, so `TILE_FUNCTIONS` and `layers.json`
  still name one relation for this channel — but `layers.json` documents
  the parameter, because the contract did change.

### `serving_geo.aggregation_overlay_tile(z, x, y, query_params json)`

The observation choropleth (r021; `aoi_id` r023, uuid guard r024, z8 floor
r025). Martin publishes it at `GET /aggregation_overlay_tile/{z}/{x}/{y}?…`,
the API at `GET /tiles/aggregation_overlay_tile/{z}/{x}/{y}?…`. One MVT layer,
`aggregation_overlay`.

- Required query params: `attribute_key` and `feature_type_slug` — without
  either the tile is empty. It reads current rows of
  `curated_geo.observation` (`is_current`) for that key on features of that
  type, so it serves raw observations and recipe outputs alike.
- Optional query param: `aoi_id`, the same `EXISTS` scoping as the other two.
- Emits per feature: `feature_id`, `type_slug`, `attribute_key`,
  `value_numeric`.
- Empty below z8, with the same simplification ladder as
  `indicator_overlay_tile`.
- Not in `layers.json`. `/tile-sources` builds its entries from the database
  (see "Source classification") and appends `v=<epoch of the key's latest
  current-observation update>` to each URL. The function ignores `v`; it
  changes the URL when the key's observations change, so a cached tile is
  not reused for new values. The entries advertise `minzoom: 8` (`OVERLAY_MIN_ZOOM`, the functions' floor since r025). The
  explore map (`installAggregationOverlays` in `apps/web/src/layers.ts`)
  appends the active `aoi_id`, replaces that floor with its baseline floor
  for the target grain (`minzoomForGrain`), and colours the layer over the
  entry's `value_range` p05–p95 through `aggregationRampStops(lo, hi)`, which
  widens a flat or inverted range so the ramp stays strictly ascending.

## Source classification

The infrastructure-owned layer manifest lives at:

- `infra/tiles/layers.json`

Each declared layer is classified as:

- `live_mvt`
- `pmtiles_snapshot`

In v1:

- reference/context layers may be `pmtiles_snapshot` (`admin_reference`)
- canonical layers under active ingestion stay `live_mvt` (`baseline_live`)
- scenario and indicator overlays stay `live_mvt` (`indicator_overlay`;
  `scenario_overlay` is declared with `implemented: false`, reserved)

`/tile-sources` (and `tiles sources`) also returns two kinds the manifest
does not declare, resolved from the database on each call (cached in the API
process for 30 s):

- `aggregation_overlay` — one entry per enabled aggregation recipe
  (`aggregation_overlay::<recipe id>`) and one per remaining
  `(attribute_key, feature type)` pair with current observations
  (`observation::<attribute_key>::<type>`), all served by
  `aggregation_overlay_tile`
- `raster_cog` — one per published raster layer, served by titiler under the
  API's `/cog`

## Dev infrastructure

- live MVT under `make dev`: the API's own route, through the web dev
  server's `/api` proxy (`REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles`)
- live MVT with Martin: `http://localhost:3000`, started by
  `infra/docker/docker-compose.yml` (`make stack-up`) against
  `postgres:5432` on the compose network

⚠ **Martin's tile cache is disabled** (`cache_size_mb: 0` in
`infra/docker/martin.yaml`). Its 512 MB default is right for static data and
wrong here: indicators are recomputed constantly and the cache cannot see
that the rows changed. Measured — after computing an indicator, a tile
requested *before* the compute kept returning `204 Empty` while the
identical call in SQL returned bytes, and neighbouring tiles never
previously requested returned data correctly. The failure mode is the worst
available: not an error, just the pre-compute answer, indistinguishable
from "this indicator produced nothing". It caused three false diagnoses in
one day. Cost of disabling is a re-query per tile, ~230 ms on the heaviest
real block tile. Raise it for a deployment where the data is not moving
under the reader, and carry the setting if you start Martin from another
config.

- PMTiles static serving: file-backed HTTP on `http://localhost:3001` (the
  compose file's `pmtiles_static` nginx service;
  `REIMAGINA_TILE_SNAPSHOT_BASE_URL` points `/tile-sources` at it)
- PMTiles export output directory: `data/tiles/pmtiles`

## CLI

```bash
python -m apps.cli.app tiles manifest
python -m apps.cli.app tiles export admin_reference
python -m apps.cli.app tiles sources
python -m apps.cli.app tiles snapshots
```

Runtime note:

- `tiles export` requires `ogr2ogr` with PostgreSQL and PMTiles driver support (GDAL ≥ 3.8)
- local sandboxed environments may block the export subprocess from connecting to Postgres over TCP even when the CLI code is correct
- macOS GDAL builds often lack the SQLite dialect used by the PMTiles driver's post-write step, so `ogr2ogr` exits non-zero (`OGR2SQLITE_Setup() failed`) even when a valid artifact is written. `reimagina.tiles.export._run_export_command` detects this pattern + verifies the artifact exists on disk before swallowing the exit code, so the flow works on macOS dev boxes. Any other non-zero exit re-raises, and the subprocess has a 30-minute timeout. The Postgres password reaches `ogr2ogr` as `PGPASSWORD`, never in its argv.
- the integration test that runs a real `ogr2ogr` skips when `ogr2ogr` is not on `PATH`. `.github/workflows/ci.yml` installs `libgdal-dev` (best-effort), not `gdal-bin`, which is the package that ships `ogr2ogr`; another test in the same file covers the export with `_run_export_command` stubbed

## Metadata surface

PMTiles snapshots are tracked in:

- `curated_geo.tile_snapshot_registry`

Each row records:

- `layer_id`
- `artifact_path`
- `artifact_uri`
- `snapshot_version`
- `source_relation`
- `source_lineage_ref`
- `status`
- `created_at`

## Rule of thumb

If a layer must reflect database changes immediately, it must stay on the live MVT path.
If snapshot freshness is acceptable and cheap/static distribution matters more, export it to PMTiles.
