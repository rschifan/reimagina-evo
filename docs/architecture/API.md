# Read-side API

FastAPI + uvicorn service that exposes the curated data surface for
browsers, analytical scripts, and anything else that wants to read the
reimagina platform without talking to Postgres directly.

- **OpenAPI**: `http://localhost:8001/docs` (Swagger) or `/redoc`.
- **Run locally**: `python -m apps.api`.
- **Base URL in dev**: `http://127.0.0.1:8001`. Through the Vite dev
  proxy the frontend sees it at `/api/...` instead.

The API owns no SQL. Every route goes through `reimagina.storage`
repositories so the architectural rule ("app code imports reimagina.*;
domain packages don't import app code") holds.

---

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/health/live` | `{status:"ok"}` — process alive. Never touches DB. |
| GET | `/health/ready` | `{status, db}` — 200 iff DB is reachable, 503 otherwise. |
| GET | `/health` | Alias for `/health/ready` (legacy). |
| GET | `/indicators` | Every indicator in `indicator_catalog`. |
| GET | `/indicators/{slug}` | One indicator's full metadata. 404 on unknown slug. |
| GET | `/indicators/{slug}/values` | Current `indicator_result` rows, optional `scenario_id`, `limit=1..10000` (default 1000). |
| GET | `/indicators/{slug}/stats` | Count, percentiles and `sum` over the scoped population (`sum` is `null` with no rows — no values is no total). `bins=1..200` adds a `histogram` of equal-width counts over the **full** observed range — the ramp clips to p05–p95 so one outlier cannot flatten the colour scale, and a histogram that clipped the same way would hide what it exists to show. `null` when there is nothing to bin. |
| GET | `/indicators/{slug}/values.csv` | Streaming CSV with pinned columns. |
| GET | `/indicators/{slug}/values.geojson` | EPSG:4326 FeatureCollection. |
| GET | `/indicators/{slug}/values.parquet` | GeoParquet byte payload (CRS=4326). |
| GET | `/aois` | All AOIs. |
| GET | `/aois/{aoi_id}/summaries` | Cached `aoi_indicator_summary` rows. 422 on malformed UUID. |
| GET | `/features` | Paginated spatial-feature inventory via `ServingReader.get_features`. |
| GET | `/tile-sources` | Resolved `infra/tiles/layers.json` joined with snapshot registry; frontend-ready. |
| GET | `/tile-sources/attribution` | Credit lines for every source that actually has features, from `data_source_catalog.license`. Derived rather than declared: the street layer is OpenStreetMap in a pilot and a generated grid in a demo, so a hardcoded credit would be a false provenance claim. Registered-but-unloaded sources are excluded. |
| GET | `/tiles/{function}/{z}/{x}/{y}` | Live MVT from a `serving_geo` tile function, behind the same URL contract Martin serves. `200` with features, `204` empty, `304` on a matching `If-None-Match`, `422` on an unaddressable coordinate, `404` outside the allowlist. See `docs/architecture/TILE_SERVING.md`. |
| GET | `/tiles/{function}` | TileJSON 3.0.0 for that function, so the route is drop-in for `tilejson_url` as well as `tiles_url_template`. |
| GET | `/scenarios` | Scenarios, newest first, by `project_id` and/or `aoi_id`. Each row carries `edit_count` and `latest_run` (`{id, status}` of its newest run envelope in `orchestration_run` — the id `GET /jobs/{run_id}` takes — or `null`) — a failed recompute still sets `materialized_at`, so only the run tells it from a good one. A run still queued has no envelope yet. The other scenario routes are in `SCENARIOS_LAYER.md`. |
| GET | `/scenarios/indicators` | The indicators a scenario rule can change, inputs before dependents, each with `statistic` (`sum` or `median`). Derived from the catalog, not listed. Declared before `/scenarios/{scenario_id}`, which would otherwise read `indicators` as an id. |
| POST | `/jobs` | Submit a run. Scope it with `changed_attributes`, `changed_datasets`, `indicators`, or nothing at all (recompute what is stale). `202` with the run id, or `status:"noop"` and a **null** run id when the dirty set was empty. |
| GET | `/jobs` | Run list for the dashboard: rows with unit counts, `total`, unfiltered `status_counts`, executor health and `server_now`, in one response. Repeatable `status=`, `limit=1..200`, `offset`. |
| GET | `/jobs/{run_id}` | Run status through the orchestrator port. Unchanged. |
| GET | `/jobs/{run_id}/detail` | Run plus per-unit timings and in-run prerequisites. Falls back to the port during the pre-envelope window. |
| GET | `/jobs/{run_id}/events` | SSE progress stream, 15 s keep-alive, 300 s cap. |
| POST | `/jobs/preview` | Dry-run the planner: which indicators *would* recompute, in dependency order, and why. Persists nothing. |
| GET | `/jobs/attributes` | What a change-set can name — attributes with their dataset, whole-dataset edges, and datasets no indicator consumes. Derived from the registry. |
| POST | `/jobs/{run_id}/cancel` | Stop a live run. 404 unknown, 409 already finished. |
| POST | `/jobs/{run_id}/rerun` | New run scoped to this run's non-succeeded indicators. 409 if live or wholly successful. |
| DELETE | `/jobs/{run_id}` | Forget one finished run (envelope + units). 409 if live. |
| DELETE | `/jobs` | Forget every finished run; returns how many. |

**Runs: what deleting does and does not touch.** `DELETE` removes the run
envelope and, by the r028 cascade, its `job_unit` rows — the dashboard's
history. It does not touch `indicator_result` or the `job_run` provenance:
forgetting that a run happened is not undoing what it produced. The DBOS system
tables keep their own record, so `GET /jobs/{run_id}` can still resolve a deleted
run from the engine plane.

⚠ **The mutating routes are unauthenticated**, like every other route here. That
is survivable while `REIMAGINA_API_HOST` stays at its `127.0.0.1` default, and
is not once the API is bound to an interface anything else can reach: `POST
/jobs` spends real compute and `DELETE /jobs` clears run history for anyone who
can reach the port. Authentication is the next slice; until it lands, do not
expose this API beyond loopback.

Download filters (`/values.csv`, `/values.geojson`, `/values.parquet`):

- `scenario_id` (uuid, optional) — base city if omitted
- `feature_type` (string, optional) — e.g. `block`, `ageb`
- `aoi_id` (uuid, optional) — only features in that AOI, the same scope as
  `/indicators/{slug}/stats?aoi_id=`
- `limit` (int, 1..500_000; default 100_000) — hard cap so a single
  request can't monopolise memory

---

## Response envelopes

### Error responses (503 / 500)

```json
{
  "error": {
    "code": "database_unavailable",
    "message": "The database was unreachable or rejected the query.",
    "request_id": "…"
  }
}
```

Every error response echoes `X-Request-Id` in both the header and the
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
| `REIMAGINA_API_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8001` | Comma-separated origins allowed through CORS. |
| `REIMAGINA_API_HOST` | `127.0.0.1` | uvicorn bind host. |
| `REIMAGINA_API_PORT` | `8000` | uvicorn bind port. |
| `REIMAGINA_API_LOG_LEVEL` | `info` | uvicorn log level. |
| `REIMAGINA_TILE_LIVE_BASE_URL` | `http://localhost:3000/get_tile` | Base URL `/tile-sources` hands back for live MVT. Must be reachable by the end user's browser in deployed environments. **This is the whole switch between tile servers:** `http://localhost:3000` for Martin, `http://localhost:8001/tiles` for the API's own route. |
| `REIMAGINA_TILE_SNAPSHOT_BASE_URL` | `http://localhost:3001` | Static host serving PMTiles snapshot artifacts. |
| `REIMAGINA_TILE_MAX_AGE_S` | `60` | `Cache-Control: max-age` on API-served tiles. |
| `REIMAGINA_TILE_STALE_WHILE_REVALIDATE_S` | `300` | `stale-while-revalidate` on API-served tiles — what lets a map pan instead of blink. |
| `REIMAGINA_TILE_TIMEOUT_MS` | `5000` | Per-request `statement_timeout` for an API-served tile, so a pathological tile fails its own request rather than holding a pooled connection. |

---

## Connection lifecycle

- A process-wide `psycopg2.pool.ThreadedConnectionPool` is created at
  startup (`create_app()` lifespan) and disposed at shutdown.
- Every request checks a connection out, sets `autocommit=False`,
  runs inside a transaction, and rolls back on teardown. If the
  rollback itself fails the connection is closed instead of returned
  to the pool so the pool self-heals.
- `/health/ready` deliberately opens a fresh connection (not the pool)
  so probe failures don't exhaust capacity.

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

- **Unit (10)**: `tests/unit/test_api_exports.py` — CSV / GeoJSON /
  GeoParquet serialisers over sample rows.
- **Contract**: `tests/contract/test_repository_structure.py` — every
  Protocol method must be implemented on both backends.
- **Integration — override path (23)**:
  `tests/integration/test_api_routes.py` — FastAPI `TestClient` with
  `dependency_overrides[get_db_conn]` so routes reuse the per-test
  Postgres transaction. Seed, call, assert, rollback.
- **Integration — real connection (5)**:
  `tests/integration/test_api_real_connection.py` — boots the real
  pool + lifespan against the session-scoped migrated DB and asserts
  liveness, readiness, catalog list, rapid-fire pool survival, and
  the `psycopg2.Error → 503 envelope + request_id` path.

Every test runs under `pytest -W error` with no warning silencers.

---

## Example calls

```bash
curl -s http://localhost:8001/health/ready
# {"status":"ok","db":"reachable"}

curl -s http://localhost:8001/indicators | jq '.[].slug'

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values?limit=5' | jq '.[].value_numeric'

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values.csv?feature_type=ageb' | head

curl -s 'http://localhost:8001/indicators/urban_form_block_building_density/values.parquet?feature_type=ageb' \
  > /tmp/density.parquet
python -c "import geopandas; print(geopandas.read_parquet('/tmp/density.parquet').head())"
```
