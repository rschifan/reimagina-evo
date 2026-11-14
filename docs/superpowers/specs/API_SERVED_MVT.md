# Spec: serve live MVT from the API, and keep Martin

Give the live vector tile path a second implementation inside the API, behind
the **same URL contract Martin already serves**, so the platform is complete
without Martin and still prefers Martin in production.

**Status:** spec. Written before the code.

**Scope:** one new router (`apps/api/routers/mvt.py`), one new shared registry
of servable tile functions, tests at the unit and integration layers, and the
documentation that names the switch. No migration. No change to
`serving_geo`, `explore`, `curated_geo`, the tile SQL functions, the layer
manifest, the tile-source resolver, or the frontend.

**Anti-scope, stated so it cannot drift:**

- **Not a replacement for Martin.** Martin stays the recommended production
  path and the default in `.env.example` and compose.
- **Not a new tile URL shape.** If this invents a URL the frontend has to
  learn, it has failed.
- **Not a caching layer, a tile cache, or a CDN.** HTTP caching headers only.
- **No change to what a tile contains.** The SQL functions are the contract;
  this is transport.

**Honest scale:** one router of roughly 200 lines, a ~30-line registry, and
tests. It adds no analytical capability. What it buys is three things, and the
first is the only one that justifies the change on its own.

---

## Why

**1. The one component that has never been executed.** Everything else in
this delivery has been run: the SQL tile functions are covered by 26
integration tests, and `scripts/mvt_inspect.py` decodes their output byte by
byte. Martin itself has never run here — the sandbox refused to execute the
binary — so the live tile path is proven at the SQL layer and at the decoder
layer with an unexecuted process between them. Work 2 is accepted when "the
code builds and runs following the included README". A README step that has
never run is the weakest sentence in the delivery.

After this change the live tile path is end-to-end testable in suites that
already run on every commit, and Martin becomes an optimisation rather than a
load-bearing dependency.

**2. It makes the vector path consistent with the raster path.** This is not
a new architectural idea in this repository — it is the one already taken.
`apps/api/app.py` mounts titiler's COG endpoints *inside the API*:

> Raster tiles: mount titiler's standard COG endpoints under `/cog` so the
> API + tile server share a process, a dep tree, and an origin. Splitting
> titiler into a sidecar (separate uvicorn or Docker) is a later scaling
> decision — until then, one less moving part.

Raster tiles are served by the API; vector tiles require a separate service.
That asymmetry has no justification behind it, only history. This removes it.

**3. It closes a bug class rather than an instance.** `/health/tile-server`
exists solely because the API and Martin read two different `DATABASE_URL`s
and can therefore disagree about which database they serve. The comment there
records that the multi-site work shipped twice against mismatched databases.
A deployment that does not run Martin cannot have that bug.

**The test for whether this is worth doing properly:** *if switching between
Martin and the API is anything more than changing one environment variable,
this is not worth building.* A second tile implementation that the frontend,
the manifest or the resolver has to know about is a liability — two code paths
to keep in step, and the one that is not in production rots. One variable, or
nothing.

---

## The contract, which already exists

All three tile functions share Martin's function-source signature, verified
against the live database rather than read off a migration:

```
aggregation_overlay_tile | z integer, x integer, y integer, query_params json DEFAULT '{}'
get_tile                 | z integer, x integer, y integer, query_params json DEFAULT '{}'
indicator_overlay_tile   | z integer, x integer, y integer, query_params json DEFAULT '{}'
```

`RETURNS bytea`, `STABLE`, `PARALLEL SAFE`. Martin's whole job is to expose
that signature at `/{function}/{z}/{x}/{y}` and forward the query string as
`query_params`. So the signature — not Martin — is the real contract, and a
second implementation of it needs no new concepts.

`reimagina/tiles/sources.py` already builds live tile URLs as
`{base}/{function}/{z}/{x}/{y}`, where `base` comes from
`REIMAGINA_TILE_LIVE_BASE_URL`. Its `_martin_base_and_default_function` treats
a final path segment as a function name only when it is one of three known
names, and otherwise keeps the whole URL as the base. Checked:
`http://localhost:8001/tiles` resolves to base `http://localhost:8001/tiles`
with default function `get_tile`, which is exactly right.

**Therefore the switch is:**

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:8001/tiles   # API serves tiles
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000         # Martin serves tiles (default)
```

No frontend change, no manifest change, no resolver change. That the resolver
already supports this is luck we are choosing to use rather than design we are
adding.

---

## Measurements that shaped the design

Taken on this container's database (the synthetic territory: 44 buildings, 16
blocks, 4 AGEBs), z16 over the populated extent. Small, and the numbers are
reported as such.

| | |
|---|---|
| Populated tile, raw | 4,101 B |
| Same tile, gzip -6 | 2,203 B (**1.86x**) |
| `get_tile` p50 | **1.50 ms** |
| `get_tile?type_slug=building` p50 | 1.10 ms |

Three consequences:

**The SQL is not the bottleneck at this scale.** 1.5 ms per tile means the
"FastAPI is too slow for tiles" objection does not bite here; HTTP overhead
and transfer dominate. It will bite at city scale with many concurrent
panning clients, which is precisely why Martin stays the default rather than
being deleted.

**Conditional requests are the highest-value header, not compression.** gzip
buys 1.86x, not the 5x people expect, because MVT is already a packed
protobuf with varint-delta geometry. A `304 Not Modified` buys 100%. So
`ETag` + `If-None-Match` is in scope and a tile cache is not.

**An empty tile is zero-length, not NULL.** Verified:
`serving_geo.get_tile(14,0,0)` returns `length = 0`, `IS NULL = false` — the
`ST_AsMVT` aggregate over an empty set returns empty bytes. This matters
because the correct HTTP answer for an empty tile is `204 No Content`, and a
route that tested `IS NULL` would return 200 with a zero-byte body forever.
Pinned in a test.

---

## Design

### Routes

| | |
|---|---|
| `GET /tiles/{function}/{z}/{x}/{y}` | MVT bytes |
| `GET /tiles/{function}` | TileJSON 3.0.0, so the route is drop-in for `tilejson_url` too and not only for `tiles_url_template` |

Every query parameter other than the path parameters is forwarded as
`query_params`, exactly as Martin does.

### The function name is an allowlist, not a parameter

A route that takes a function name from the URL and calls it is an RPC
endpoint into Postgres. The repository already has the precedent for refusing
that: `cog_security.validated_cog_path` exists because titiler's default
would hand any `url` straight to GDAL and let a client read
`file:///etc/passwd`. This is the same hazard in a different costume.

Two gates, both required:

1. The name must be in the registry — one shared `TILE_FUNCTIONS` constant,
   seeded from the `_KNOWN_MARTIN_FUNCTION_TAILS` frozenset that
   `sources.py` already keeps, and **moved** there rather than copied, so
   there is exactly one list of servable tile functions in the codebase.
2. The call is built with `psycopg2.sql.Identifier`, never string
   interpolation, so even a registry mistake cannot become injection.

### HTTP semantics, which are the actual work

| Condition | Response |
|---|---|
| Tile has features | `200`, `application/vnd.mapbox-vector-tile` |
| Tile is empty (0 bytes) | `204 No Content` |
| `If-None-Match` matches | `304 Not Modified` |
| `z` outside 0–24, or `x`/`y` outside `0 … 2^z − 1` | `422` before touching the database |
| Unknown function | `404` |

- **ETag** is a strong validator over the tile bytes, with the content
  encoding folded in (`"<sha256-prefix>"` vs `"<sha256-prefix>-gzip"`) and
  `Vary: Accept-Encoding`, so a shared cache cannot serve a gzip body to a
  client that asked for identity.
- **Cache-Control** `public, max-age=…, stale-while-revalidate=…`,
  configurable, defaulting to 60 s / 300 s. Public is correct even for
  scenario overlays: a scenario tile varies by `scenario_id`, which is in the
  query string, so a shared cache keys on it already. Nothing here is
  per-user.
- **gzip** only when the client accepts it *and* the tile is at least 1 KiB —
  below that the compression costs more than it saves.
- **Bounds are validated before the query.** `ST_TileEnvelope` accepts
  nonsense and a client walking `z=30` would otherwise turn a typo into
  database load.

### Safety on the connection

Each tile request runs with a per-statement `statement_timeout` (default
5 s, configurable) in a **read-only** transaction. The functions are `STABLE`,
so read-only costs nothing and removes a class of accident outright. A
pathological tile fails its own request instead of holding a pooled
connection.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | the registry is the single source of truth and agrees with `sources.py`; z/x/y bounds; ETag/encoding pairing; gzip threshold; unknown function → 404 |
| `tests/integration` | real database, real bytes: a populated tile decodes to the expected layer and feature count through `scripts/mvt_inspect.py`'s decoder; an empty tile is `204`; `If-None-Match` round-trips to `304`; `type_slug` filtering changes the decoded output; all three functions are reachable |
| `tests/contract` | the API-served base URL resolves through `resolve_tile_sources` to the same URL shape Martin's does — the one-variable-switch claim, asserted rather than described |

---

## What the maintenance checklist asks for

`AGENTS.md` step by step: no migration (1 skipped), no change to the
app-facing contract in `serving_geo` (2 skipped), no change to the
engineer-facing `explore` surface (3 skipped), CLI inspection still reads
`explore` (4 unaffected), tests added (5), and `AGENTS.md`,
`infra/tiles/layers.json` and the delivery documentation updated where the
contract moved (6). `layers.json` itself does **not** change: the layers are
unchanged, only the base URL they resolve against, which is configuration.

---

## What the implementation found

Four things, appended as the design record rather than folded back into the
plan above.

### 1. `SET LOCAL` is transaction-scoped, so the first version was destructive

The route set `transaction_read_only = on` and then called
`conn.rollback()`. That is correct for a request that owns its connection,
which is how the API's pool hands one out — and quietly destructive
otherwise. Anything sharing that transaction afterwards failed with `cannot
execute INSERT in a read-only transaction`, and the bare `rollback()` would
have discarded a caller's uncommitted work besides.

The integration suite found it on its first run, because it pins the API to
its own seeding transaction. The fix is a savepoint: Postgres stacks GUC
settings per subtransaction, so `ROLLBACK TO SAVEPOINT` releases both `SET
LOCAL`s and leaves an outer transaction untouched. Two extra statements buy a
tile read that is genuinely self-contained — safe on a pooled connection,
safe inside a caller's unit of work, read-only either way — and the release
is in a `finally`, so one failed tile cannot leave the rest of the
transaction unable to write.

The original design note said read-only "costs nothing". It cost correctness
until it was scoped.

### 2. An absolute tile URL silently depends on the CORS allowlist

The switch works exactly as designed at the resolver: pointing
`REIMAGINA_TILE_LIVE_BASE_URL` at `http://localhost:8001/tiles` repoints all
three live layers, and every URL it advertises answers. In a **browser** it
drew nothing.

Measured, in a real browser, with the route working perfectly:

```
requested: 53 | responded: 0 | failed: 53
net::ERR_FAILED  http://127.0.0.1:8001/tiles/get_tile/8/45/103?type_slug=block&…
```

The API's default CORS allowlist is `http://localhost:5173`, and the page had
been opened as `http://127.0.0.1:5173` — a different origin. No console error
named CORS; the map simply stayed empty, which is indistinguishable from "no
data loaded".

The answer was already in the repository: the web app proxies `/api` to the
API, so a **relative** base makes tiles same-origin and removes the question.
Verified from an origin deliberately absent from the allowlist:

```
requested: 42 | responded: 38 | failed: 4   (4 = MapLibre cancelling on pan)
http://127.0.0.1:5173/api/tiles/get_tile/10/183/413?… → 200
```

So the documented form is `REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles`, and the
absolute form is kept as the second option with the caveat attached. A
contract test pins the resolver behaviour that makes the relative form work.

### 3. The measurement nearly produced a false finding

The first browser probe counted Playwright `response` events and reported
zero tile requests under the API configuration — which reads as "the frontend
never asked for a tile". It had asked 53 times; a request that fails emits
`requestfailed` and **no** `response`, so the probe was blind to exactly the
case it was measuring. Counting `request`, `response` and `requestfailed`
separately is what turned "the frontend is broken" into "the allowlist
rejects this origin".

Recorded because the wrong version of that probe would have produced a
confident, wrong, and expensive conclusion about the frontend.

### 4. `/tiles` collides with an existing frontend heuristic, harmlessly

`layers.ts::_deriveCogEndpointBase` derives the titiler base by slicing a
raster tile template at `"/tiles/"`. An API-served **vector** template now
contains that substring too. It is not reached — that function runs only over
raster sources — so nothing breaks, but the collision is one rename away from
mattering and is noted here rather than discovered later.

---

## Verified

- `tests/unit/test_mvt_route.py` — 46 tests: allowlist, coordinate bounds,
  204/304/404/422, encoding-paired validators, savepoint scoping.
- `tests/integration/test_mvt_route_serves_tiles.py` — 14 tests against real
  PostGIS, decoding real tile bytes with the in-tree decoder. No optional
  dependency, so nothing skips.
- `tests/contract/test_repository_structure.py` — the one-variable switch,
  the single-registry property, and the relative-base resolution.
- A real browser rendering the synthetic Mexicali territory from
  `/api/tiles/...`: 8 tiles with features, 20 empty, map drawn.
