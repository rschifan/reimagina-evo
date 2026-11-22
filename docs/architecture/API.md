# Read-side API

FastAPI + uvicorn service that exposes the curated data surface for
browsers, analytical scripts, and anything else that wants to read the
reimagina platform without talking to Postgres directly.

- **OpenAPI**: `http://localhost:8001/docs` (Swagger) or `/redoc`.
- **Run locally**: `make dev` (API, web app and worker), or `uv run python -m apps.api`.
- **Base URL in dev**: `http://127.0.0.1:8001`. Through the Vite dev
  proxy the frontend sees it at `/api/...` instead.

The API owns no domain SQL. Routes go through `reimagina.storage`
repositories or the domain packages behind them (`reimagina.scenarios`,
`reimagina.orchestration`, `reimagina.tiles`, `reimagina.data`), so the
architectural rule ("app code imports reimagina.*; domain packages don't
import app code") holds. The only statements written in `apps/api` are the
health probes (`SELECT 1`, `SELECT current_database()`) and the MVT route's
call of an allowlisted `serving_geo` tile function.

---

## Endpoints

All 44 operations the project defines are below; the in-process titiler routes under `/cog` are titiler's own. The OpenAPI document (`/openapi.json`, rendered at `/docs`) carries every parameter and response model, the titiler routes included.

| Method | Path | Returns |
|---|---|---|
| GET | `/health/live` | `{status:"ok"}` — process alive. Never touches DB. |
| GET | `/health/ready` | `{status, db}` — 200 iff DB is reachable, 503 otherwise. |
| GET | `/health` | Alias for `/health/ready` (legacy). |
| GET | `/health/tile-server` | Whether Martin answers at `REIMAGINA_MARTIN_URL` (its `/catalog`), and the database the API itself reads, so the two can be compared: `{status, martin, api_database, martin_reachable, martin_url, notes}`. 200 iff Martin answers, 503 otherwise. |
| GET | `/indicators` | Every indicator in `indicator_catalog`. Optional `aoi_id` keeps only indicators with at least one current value in that AOI; `scenario_id` (only with `aoi_id`) counts that scenario's rows as well as the baseline's. Each row carries `cap_value` — the value at which a distance indicator is truncated (e.g. `1500` m) — or `null` when it has none; a reading at the cap means "this far or more", not a measurement. |
| GET | `/indicators/{slug}` | One indicator's full metadata. 404 on unknown slug. |
| GET | `/indicators/{slug}/dependencies` | What the indicator is computed from (`upstream`) and what is computed from it (`downstream`), indicator to indicator: `{slug, upstream, downstream}`, each edge with names, the input's unit and `weight`. Both empty is the common case. 404 on unknown slug. |
| GET | `/indicators/{slug}/lineage` | The asset closure around `indicator.{slug}` in both directions, down to the datasets it ultimately reads; each node with `kind`, `depth` and freshness `state`. Empty lists when the asset graph is not seeded. 404 on unknown slug. |
| GET | `/indicators/{slug}/values` | Current `indicator_result` rows ordered by `feature_id`, optional `scenario_id`, `limit=1..10000` (default 1000), keyset `after_feature_id` (last `feature_id` of the previous page). `X-Truncated: true` means more rows follow. 404 on unknown slug. |
| GET | `/indicators/{slug}/stats` | Count, percentiles and `sum` over the scoped population (`scenario_id`, `feature_type`, `aoi_id`, all optional; `sum` is `null` with no rows — no values is no total). `bins=1..200` (default 0, none) adds a `histogram` of equal-width counts over the **full** observed range — the ramp clips to p05–p95 so one outlier cannot flatten the colour scale, and a histogram that clipped the same way would hide what it exists to show. `null` when there is nothing to bin. `count_at_cap` is the number of those rows with `value >= cap_value`, `null` when the indicator has no cap. |
| GET | `/indicators/{slug}/values.csv` | Streaming CSV with pinned columns. |
| GET | `/indicators/{slug}/values.geojson` | EPSG:4326 FeatureCollection. |
| GET | `/indicators/{slug}/values.parquet` | GeoParquet byte payload (CRS=4326). |
| GET | `/aois` | All AOIs: `aoi_id`, `project_id`, `name`, `description`, `bbox`, `centroid`, `features_bbox`. Cached in-process for 30 s. |
| GET | `/aois/{aoi_id}/summaries` | Cached `aoi_indicator_summary` rows; optional `scenario_id`, `only_current` (default `true`). 422 on malformed UUID, 404 on unknown AOI. |
| GET | `/features` | Paginated spatial-feature inventory via `ServingReader.get_features` (no geometry). Optional `type_slug`, `role`, `aoi_id`, `limit=1..5000` (default 500), `after_cursor`; 422 on a malformed cursor. |
| GET | `/features/{feature_id}/indicators` | The feature's current value for every indicator that has one, each with its exact rank among same-grain features of the AOI: `slug`, `name`, `unit`, `cap_value`, `value`, `count_below`, `count_equal`, `count`. Optional `aoi_id` (the peer set) and `scenario_id` (base city if omitted). 404 on an unknown feature. |
| POST | `/metrics/within-polygon` | Per-type counts of the AOI's features whose centroid falls inside a GeoJSON Polygon. Body: `aoi_id`, `polygon` (EPSG:4326), `type_slugs`. Returns `{results: [{type_slug, count}]}`, one row per requested slug. 422 on a non-Polygon or non-finite polygon. |
| POST | `/metrics/indicator-within-polygon` | `/indicators/{slug}/stats` for the features whose centroid falls inside a GeoJSON Polygon (baseline values). Body: `aoi_id`, `polygon` (EPSG:4326), `indicator_slug`, optional `feature_type`. Same response shape as `/stats`, including `count_at_cap`. 422 on a non-Polygon or non-finite polygon, 404 on an unknown indicator. |
| GET | `/tile-sources` | Resolved `infra/tiles/layers.json` joined with the snapshot registry and the published raster layers; frontend-ready. Optional `live_tilejson_base_url`, `snapshot_base_url`, `raster_base_url` override the environment defaults. Cached in-process for 30 s. |
| GET | `/tile-sources/attribution` | Credit lines for every source that actually has features or a published raster layer, from `data_source_catalog.license`. Derived rather than declared: the street layer is OpenStreetMap in a pilot and a generated grid in a demo, so a hardcoded credit would be a false provenance claim. Registered-but-unloaded sources are excluded. |
| GET | `/tiles/{function}/{z}/{x}/{y}` | Live MVT from a `serving_geo` tile function, behind the same URL contract Martin serves. `200` with features, `204` empty, `304` on a matching `If-None-Match`, `422` on an unaddressable coordinate, `404` outside the allowlist. See `docs/architecture/TILE_SERVING.md`. |
| GET | `/tiles/{function}` | TileJSON 3.0.0 for that function, so the route is drop-in for `tilejson_url` as well as `tiles_url_template`. |
| GET | `/scenarios` | Scenarios, newest first, by `project_id` and/or `aoi_id`. Each row carries `edit_count` and `latest_run` (`{id, status}` of its newest run envelope in `orchestration_run` — the id `GET /jobs/{run_id}` takes — or `null`) — a failed recompute still sets `materialized_at`, so only the run tells it from a good one. A run still queued has no envelope yet. `SCENARIOS_LAYER.md` describes the scenario routes in depth. |
| GET | `/scenarios/indicators` | The indicators a scenario rule can change, inputs before dependents, each with `statistic` (`sum` or `median`). Derived from the catalog, not listed. Declared before `/scenarios/{scenario_id}`, which would otherwise read `indicators` as an id. |
| POST | `/scenarios` | Create a named what-if. Body: `project_id`, `name`, optional `aoi_id`, `description`. `201` with the scenario as `GET` returns it; 409 on a duplicate name in the project. |
| GET | `/scenarios/{scenario_id}` | One scenario with its `edits` in ordinal order and its `override_count`. 404 for an unknown id. |
| DELETE | `/scenarios/{scenario_id}` | Delete a scenario and everything it wrote; answers the four counts removed (`overrides`, `versions_retired`, `results_superseded`, `materializations`). 404 for an unknown id. |
| POST | `/scenarios/{scenario_id}/edits` | Draw one area and write one rule over it, in one transaction: one edit per number set (`levels` writes both `floors` and `num_levels`, so up to five edits for `levels`, `unit_size`, `people_per_household`, `lot_coverage`). `201` with the edits written; 400 on no number, a non-positive value or an invalid area; 422 on a value past the density ceiling. |
| PATCH | `/scenarios/{scenario_id}/rules` | Change one rule's numbers (`edit_ids`, `levels`, `unit_size`, `people_per_household`, `lot_coverage`), and optionally move or resize it (`area`), in place. Same refusals, in the same order, as `POST …/edits`; does not recompute. |
| DELETE | `/scenarios/{scenario_id}/edits` | Remove one edit (`?edit_id=`) from this scenario; 404 if it is not one of its edits. Its overrides are withdrawn on the next recompute. `204`. |
| POST | `/scenarios/{scenario_id}/recompute` | Materialise the scenario's edits into overrides, then enqueue the recompute of what they changed. `202` with the run id, `null` when nothing needs recomputing. |
| GET | `/scenarios/{scenario_id}/comparison` | One indicator under the scenario against the baseline (`?indicator_slug=`, optional `aoi_id`, `limit`): `{feature_id, baseline, scenario, delta, state}` per feature, `state` one of `changed`, `unchanged`, `new`. |
| POST | `/jobs` | Submit a run. Body: `scenario_id`, `aoi_id`, and a scope — `changed_attributes` and/or `edited_features`, `changed_datasets`, `indicators`, or nothing at all (recompute what is stale). `202` with `{run_id, status, deduplicated}`, or `status:"noop"` and a **null** run id when the dirty set was empty. `wait: true` blocks until the run is terminal and answers 200, or 504 after 300 s. 422 on an unknown indicator slug. |
| GET | `/jobs` | Run list for the dashboard: rows with unit counts, `total`, unfiltered `status_counts`, executor health and `server_now`, in one response. Repeatable `status=`, `limit=1..200` (default 50), `offset`. |
| GET | `/jobs/{run_id}` | Run status and per-unit status through the orchestrator port. 404 on an unknown run. |
| GET | `/jobs/{run_id}/detail` | Run plus per-unit timings and in-run prerequisites. Falls back to the port during the pre-envelope window. 404 on an unknown run. |
| GET | `/jobs/{run_id}/events` | SSE progress stream, 15 s keep-alive, 300 s cap. 404 on an unknown run. |
| POST | `/jobs/preview` | Dry-run the planner: which indicators *would* recompute, in dependency order, and why. Persists nothing. |
| GET | `/jobs/attributes` | What a change-set can name — attributes with their dataset, whole-dataset edges, and datasets no indicator consumes. Derived from the registry. |
| POST | `/jobs/{run_id}/cancel` | Stop a live run. 404 unknown, 409 already finished. |
| POST | `/jobs/{run_id}/rerun` | New run scoped to this run's non-succeeded indicators; 202 with `rerun_of` and `indicators`. 404 unknown, 409 if live or wholly successful. |
| DELETE | `/jobs/{run_id}` | Forget one finished run (envelope + units). 404 unknown, 409 if live. |
| DELETE | `/jobs` | Forget every finished run; returns how many. |
| GET, POST | `/cog/...` | titiler's `TilerFactory` COG endpoints, mounted in-process: `info`, `info.geojson`, `statistics`, `tiles`, `tiles/{tileMatrixSetId}`, `tiles/{tileMatrixSetId}/{z}/{x}/{y}[.{format}]`, `{tileMatrixSetId}/map.html`, `{tileMatrixSetId}/tilejson.json`, `point/{lon},{lat}`, `preview[...]`, `bbox/...`, `feature[...]`. The `url` parameter must be a `file://` path under the data root (`REIMAGINA_DATA_DIR`, default `<repo>/data`): another scheme, an authority or no path is 400, a missing file 404, a path outside the root or not a regular file 403 (`apps/api/cog_security.py`). |

The scenario routes' bodies, responses and refusals are in
`SCENARIOS_LAYER.md` — *The HTTP surface*.

**Runs: what deleting does and does not touch.** `DELETE` removes the run
envelope and, by the r028 cascade, its `job_unit` rows — the dashboard's
history. It does not touch `indicator_result` or the `job_run` provenance:
forgetting that a run happened is not undoing what it produced. The engine
forgets the run too — its DBOS workflows, children included, are deleted before
the commit — so a deleted run answers 404 on `GET /jobs/{run_id}` rather than
coming back from the engine plane.

⚠ **The mutating routes are unauthenticated**, like every other route here. That
is survivable while the API stays bound to `127.0.0.1` (the default of
`REIMAGINA_API_HOST`, and what `make dev` passes to uvicorn), and is not once it
is bound to an interface anything else can reach: `POST /jobs` spends real
compute, `DELETE /jobs` clears run history and the scenario routes write and
delete scenarios for anyone who can reach the port. Authentication is the next
slice; until it lands, do not expose this API beyond loopback.

Download filters (`/values.csv`, `/values.geojson`, `/values.parquet`):

- `scenario_id` (uuid, optional) — base city if omitted
- `feature_type` (string, optional) — e.g. `block` (every shipped indicator
  is block-grain)
- `aoi_id` (uuid, optional) — only features in that AOI, the same scope as
  `/indicators/{slug}/stats?aoi_id=`
- `limit` (int, 1..500_000; default 100_000) — hard cap so a single
  request can't monopolise memory. Rows are ordered by `feature_id`; the
  response carries `X-Row-Limit` and `X-Truncated: true|false`, so a file
  the cap cut short is never mistaken for the whole set

---

## Response envelopes

### Error responses (503 / 500)

```json
{
  "error": {
    "code": "database_unavailable",
    "message": "The database was unreachable.",
    "request_id": "…"
  }
}
```

Four codes use it (`apps/api/errors.py`): `database_unavailable` (503,
`psycopg2.OperationalError` / `InterfaceError`), `database_error` (500, any
other `psycopg2.Error`), `internal_error` (500, anything unhandled) and
`ops_plane_not_provisioned` (503, below).

Every envelope echoes `X-Request-Id` in both the header and the
body so server logs + user-visible incidents can be correlated without
leaking stack traces.

⚠ This held only as long as `apps.api.errors.unhandled_error_handler` is the
handler Starlette actually reaches for every 5xx. titiler's
`DEFAULT_STATUS_CODES` maps four classes to 500 — `Exception`,
`RasterioIOError`, `RasterioError`, `RioTilerError` — and all four answered
`{"detail": str(exc)}` with no request id, echoing back whatever the exception
carried. `Exception` was the worst of it, because registering it replaced ours
for every route in the app; but a `RasterioIOError` carries the COG's path, and
for a remote COG the URL with any SAS token still in its query string.

`create_app` therefore keeps titiler's map only where it says something other
than "internal error" — `{exc: code for … if code < 500}`, the ten 400s and
404s that are the reason it is registered at all. Everything 5xx falls through
to the envelope above. `tests/unit/test_api_error_envelope.py` holds both
halves of that line.

### Refusals that name their own remedy (`detail`)

A route that rejects a well-formed request answers FastAPI's `{"detail": "…"}`
with a sentence meant to be shown to the operator, **not** the envelope above —
there is nothing internal in it to withhold:

| Status | Where | Means |
|---|---|---|
| 409 | `POST /jobs/{id}/cancel` | the run is already terminal |
| 409 | `POST /jobs/{id}/rerun` | the run is still live — cancel it first — or every indicator in it succeeded, so there is nothing to retry |
| 422 | `POST /jobs`, `/jobs/preview`, `/jobs/{id}/rerun` | an indicator slug no registry entry matches — for a rerun, one the original run recorded that has since been renamed or removed |
| 503 | every route that takes a pooled connection | every pooled connection stayed busy for `REIMAGINA_API_POOL_WAIT_S`; carries `Retry-After: 1` |
| 504 | `POST /jobs` with `wait: true` | the run did not finish within 300 s; it is still running |

The scenario routes answer their refusals the same way: `detail` is a sentence
for a 404 or 409, and an object — `error`, `field`, `message` and, for the
ceilings, the numbers — for a refusal a form can point at a field; see
`SCENARIOS_LAYER.md` — *The HTTP surface*.

The Ops dashboard renders `detail` when present and falls back to the
envelope's `message` plus its `request_id`, so both shapes reach the operator
as words rather than as a status line.

**One refusal does not use `detail`: the ops-plane 503.** A missing
`dbos` schema answers the envelope above, with
`code: "ops_plane_not_provisioned"` and a `message` naming `bootstrap-db` —
the one place the envelope's `message` carries a sentence meant to be read,
because it is this project's own text and not a driver's. It is registered
app-wide in `create_app` rather than caught per route, so **every** path that
submits a run answers it: `POST /jobs`, `POST /jobs/{id}/rerun` and
`POST /scenarios/{id}/recompute`. A per-route `except` covered one of the
three, and the other two answered 500 for the identical condition.

### Validation errors (422)

FastAPI's standard shape:

```json
{ "detail": [ { "loc": [...], "msg": "…", "type": "…" } ] }
```

---

## Operational knobs

All via environment variable; sensible defaults for local dev.

| Variable | Default | Purpose |
|---|---|---|
| `REIMAGINA_APP_DSN` | `postgresql://localhost/reimagina_app` | DSN the API connects to. |
| `REIMAGINA_API_POOL_MIN` | `1` | Min connections in the `psycopg2.pool.ThreadedConnectionPool`. |
| `REIMAGINA_API_POOL_MAX` | `16` | Max connections. |
| `REIMAGINA_API_POOL_WAIT_S` | `30` | How long a request waits for a free connection when all `REIMAGINA_API_POOL_MAX` are busy, before answering 503 with `Retry-After`. psycopg2's pool never waits on its own; without this a burst wider than the pool (a map loading at a new zoom) failed with `connection pool exhausted`. |
| `REIMAGINA_API_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8001` | Comma-separated origins allowed through CORS. The methods allowed cross-origin are `GET`, `POST`, `PATCH`, `DELETE` and `OPTIONS`. |
| `REIMAGINA_API_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated `Host` header values the API answers (port ignored); any other gets 400. Guards the unauthenticated, loopback-bound API against DNS rebinding. Add the hostname clients use when serving it behind a proxy or on another interface. |
| `REIMAGINA_API_HOST` | `127.0.0.1` | uvicorn bind host (`python -m apps.api`). |
| `REIMAGINA_API_PORT` | `8001` | uvicorn bind port for `python -m apps.api`. The `make` targets pass `--port $(API_PORT)` (default `8001`) instead. |
| `REIMAGINA_API_LOG_LEVEL` | `info` | uvicorn log level (`python -m apps.api`). |
| `REIMAGINA_MARTIN_URL` | `http://localhost:3000` | Where `/health/tile-server` probes Martin. |
| `REIMAGINA_TILE_LIVE_BASE_URL` | `http://localhost:3000/get_tile` | Base URL `/tile-sources` hands back for live MVT. Must be reachable by the end user's browser in deployed environments. **This is the whole switch between tile servers:** `http://localhost:3000` for Martin, `/api/tiles` for the API's own route through the web app's proxy (the absolute `http://localhost:8001/tiles` is cross-origin and depends on CORS). `make dev` and `make dev-api` set `/api/tiles` regardless of `.env`; only a value set in the invoking shell overrides them. |
| `REIMAGINA_TILE_SNAPSHOT_BASE_URL` | `http://localhost:3001` | Static host serving PMTiles snapshot artifacts. |
| `REIMAGINA_TILE_RASTER_BASE_URL` | `$REIMAGINA_API_BASE_URL/cog`, else `http://localhost:8001/cog` | Base URL `/tile-sources` hands back for raster (titiler) tiles. |
| `REIMAGINA_DATA_DIR` | `<repo>/data` | Data root; the only tree `/cog` will open a COG from. |
| `REIMAGINA_TILE_MAX_AGE_S` | `60` | `Cache-Control: max-age` on API-served tiles. Scenario tiles (`scenario_id` in the query) are always `no-cache`. |
| `REIMAGINA_TILE_STALE_WHILE_REVALIDATE_S` | `300` | `stale-while-revalidate` on API-served tiles — what lets a map pan instead of blink. |
| `REIMAGINA_TILE_TIMEOUT_MS` | `5000` | Per-request `statement_timeout` for an API-served tile, so a pathological tile fails its own request rather than holding a pooled connection. |

---

## Connection lifecycle

- A process-wide `psycopg2.pool.ThreadedConnectionPool` is created at
  startup (`create_app()` lifespan) and disposed at shutdown.
- Every request that depends on `get_db_conn` checks a connection out
  (waiting up to `REIMAGINA_API_POOL_WAIT_S` when all are busy), sets
  `autocommit=False`, runs inside a transaction, and rolls back on
  teardown. If the rollback itself fails the connection is closed
  instead of returned to the pool so the pool self-heals.
- Connection capacity is awaited asynchronously. Waiting requests do not occupy
  the thread pool needed by handlers holding connections. Socket checkout and
  rollback run off the event loop; cancellation waits for those operations to
  finish before returning capacity, so a tile burst or cancelled navigation
  cannot strand connections.
- Because that dependency rolls back on success, every route that writes
  opens its own committed connection instead: `POST /jobs`, cancel,
  rerun, both `DELETE /jobs` routes, and every `/scenarios` route (the
  reads there too, so none of them depends on the pool).
- `/health/ready` and `/health/tile-server` deliberately open a fresh
  connection (not the pool) so probe failures don't exhaust capacity.

---

## Request correlation

`X-Request-Id` is:

- Honoured on incoming requests if present.
- Generated as a UUID4 otherwise.
- Echoed in the response headers.
- Included in error envelopes.

Use it to grep server logs when a user reports a failed call.

---

## Testing

- **Unit (12)**: `tests/unit/test_api_exports.py` — CSV / GeoJSON /
  GeoParquet serialisers over sample rows. Also `test_api_error_envelope.py`
  (the envelope survives titiler's handlers), `test_mvt_route.py` and
  `test_event_stream.py` (MVT and SSE HTTP behaviour without a database).
- **Contract**: `tests/contract/test_repository_structure.py` — every
  Protocol method must be implemented on the Postgres backend.
  `test_jobs_api.py`, `test_scenarios_api.py`, `test_malformed_ids.py` and
  `test_feature_not_found.py` pin the jobs and scenario routes, malformed ids
  and the feature 404 with no database.
- **Integration — override path (57)**:
  `tests/integration/test_api_routes.py` — FastAPI `TestClient` with
  `dependency_overrides[get_db_conn]` so routes reuse the per-test
  Postgres transaction. Seed, call, assert, rollback.
- **Integration — real connection (6)**:
  `tests/integration/test_api_real_connection.py` — boots the real
  pool + lifespan against the session-scoped migrated DB and asserts
  liveness, readiness, catalog list, rapid-fire pool survival, the
  `OperationalError → 503 envelope + request_id` path and the
  `ProgrammingError → 500` path. `test_api_pool_waits.py`,
  `test_scenarios_api.py`, `test_metrics_within_polygon.py`,
  `test_mvt_route_serves_tiles.py`, `test_tile_sources_rasters.py` and
  `test_indicator_picker_scenario.py` cover the rest against real PostGIS.

CI runs these suites (`uv run pytest tests/unit tests/contract`, then
`tests/integration`) with pytest's default warning handling, not `-W error`;
nothing in `pyproject.toml` or `tests/` filters warnings.

---

## Example calls

```bash
curl -s http://localhost:8001/health/ready
# {"status":"ok","db":"reachable"}

curl -s http://localhost:8001/indicators | jq '.[].slug'

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values?limit=5' | jq '.[].value_numeric'

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values.csv?feature_type=block' | head

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values.parquet?feature_type=block' \
  > /tmp/density.parquet
uv run python -c "import geopandas; print(geopandas.read_parquet('/tmp/density.parquet').head())"
```
