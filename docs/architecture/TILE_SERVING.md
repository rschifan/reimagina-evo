# Tile Serving

Tile serving in Reimagina follows a hybrid rule:

- **live MVT** is the system of record for current, filtered, scenario-aware, or frequently changing map data
- **PMTiles** are snapshot artifacts for stable reference/context layers

## Current v1 contract

- `serving_geo.get_tile()` is the primary live tile path, served by
  **Martin** by default and by **the API itself** as an alternative — see
  "Two servers, one contract" below
- `serving_geo.indicator_overlay_tile()` adds a live choropleth channel
  (details below), published by both
- PMTiles are additive, not a replacement
- v1 PMTiles target: `admin_reference` from `serving_geo.admin_v`
- PMTiles are rebuilt manually, on schedule, or on release publication
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

Choosing between them is one environment variable, resolved by
`tiles.sources`:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000         # Martin (default)
REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles                    # the API, same-origin
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:8001/tiles   # the API, direct
```

⚠ **Prefer the relative form.** An absolute URL makes every tile request
cross-origin from the web dev server, so it depends on the API's CORS
allowlist — which defaults to `http://localhost:5173` and therefore rejects a
browser that reached the page as `http://127.0.0.1:5173`. Measured in a real
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

**The servable functions are one list**, `reimagina.tiles.functions.TILE_FUNCTIONS`,
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
API and Martin read two different `DATABASE_URL`s and can disagree about
which database they serve; its own comment records that the multi-site work
shipped twice against mismatched databases. A deployment without Martin
cannot have that bug.

### What the API route adds on top of the SQL

The bytes are one function call; the HTTP is the work.

| Condition | Response |
|---|---|
| Tile has features | `200`, `application/vnd.mapbox-vector-tile` |
| Tile is empty | `204 No Content` — matching Martin, and matching what this document already specified for sub-`z14` buildings |
| `If-None-Match` matches | `304 Not Modified` |
| `z` outside 0–24, or `x`/`y` outside `0 … 2^z − 1` | `422`, before the database is touched |
| Unknown function | `404` |

- **`ETag`** is a content hash with the encoding folded in, plus
  `Vary: Accept-Encoding`. A shared cache must not be able to serve a gzip
  body under an identity validator.
- **`Cache-Control`** `public, max-age=60, stale-while-revalidate=300`,
  configurable. `public` is right even for scenario overlays: they vary by
  `scenario_id`, which is in the query string, so a shared cache already
  keys on it.
- **gzip** above 1 KiB only. Measured: 1.86x on a real tile — MVT is already
  a packed protobuf, so a `304` is worth far more than compression, which is
  why conditional requests are in scope and a tile cache is not.
- **The read is scoped to a savepoint.** `SET LOCAL transaction_read_only`
  is transaction-scoped, so setting it on the ambient transaction and rolling
  back was fine for a request that owns its connection and destructive for
  anything sharing one. Postgres stacks GUC settings per subtransaction, so
  `ROLLBACK TO SAVEPOINT` releases the setting and leaves an outer
  transaction untouched.

`GET /tiles/{function}` returns TileJSON 3.0.0, so the route is drop-in for
the resolver's `tilejson_url` as well as its `tiles_url_template`.

Full rationale: `docs/superpowers/specs/API_SERVED_MVT.md`.

### `serving_geo.get_tile(z, x, y, query_params json)`

The baseline feature channel. Martin publishes it at
`GET /get_tile/{z}/{x}/{y}?…`; the API publishes the same at
`GET /tiles/get_tile/{z}/{x}/{y}?…`. Query params: `type_slug`, `role`, `aoi_id`
— all optional.

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
  `GET /indicator_overlay_tile/{z}/{x}/{y}?…`.
- Required query param: `indicator_slug` — the function returns an
  empty tile when absent so a misconfigured caller never leaks the
  whole table.
- Optional query param: `scenario_id` (uuid). Omit for base state.
- Returns empty below `z=10`; choropleth below city scale is misleading
  (every feature collapses into one pixel). `infra/tiles/layers.json`
  reflects this with `minzoom: 10`.
- Emits per feature: `feature_id`, `type_slug`, `value_numeric`,
  `quality_flag`. Frontend's `fill-color` expression reads
  `value_numeric`; `quality_flag` is used to dim `estimated` / `missing`
  rows if the UI wants to distinguish them.

## Source classification

The infrastructure-owned layer manifest lives at:

- `infra/tiles/layers.json`

Each declared layer is classified as:

- `live_mvt`
- `pmtiles_snapshot`

In v1:

- reference/context layers may be `pmtiles_snapshot`
- canonical layers under active ingestion stay `live_mvt`
- scenario and indicator overlays stay `live_mvt`

## Dev infrastructure

- live MVT: Martin on `http://localhost:3000`

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

- PMTiles static serving: file-backed HTTP on `http://localhost:3001`
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
- macOS GDAL builds often lack the SQLite dialect used by the PMTiles driver's post-write step, so `ogr2ogr` exits non-zero (`OGR2SQLITE_Setup() failed`) even when a valid artifact is written. `reimagina.tiles.export._run_export_command` detects this pattern + verifies the artifact exists on disk before swallowing the exit code, so the flow works on macOS dev boxes and CI alike. Any other non-zero exit re-raises.

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
