# Read-side API

FastAPI + uvicorn service that exposes the curated data surface for
browsers, analytical scripts, and anything else that wants to read the
reimagina platform without talking to Postgres directly.

- **OpenAPI**: `http://localhost:8000/docs` (Swagger) or `/redoc`.
- **Run locally**: `python -m apps.api`.
- **Base URL in dev**: `http://127.0.0.1:8000`. Through the Vite dev
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
| GET | `/indicators/{slug}/values.csv` | Streaming CSV with pinned columns. |
| GET | `/indicators/{slug}/values.geojson` | EPSG:4326 FeatureCollection. |
| GET | `/indicators/{slug}/values.parquet` | GeoParquet byte payload (CRS=4326). |
| GET | `/aois` | All AOIs. |
| GET | `/aois/{aoi_id}/summaries` | Cached `aoi_indicator_summary` rows. 422 on malformed UUID. |
| GET | `/features` | Paginated spatial-feature inventory via `ServingReader.get_features`. |
| GET | `/tile-sources` | Resolved `infra/tiles/layers.json` joined with snapshot registry; frontend-ready. |

Download filters (`/values.csv`, `/values.geojson`, `/values.parquet`):

- `scenario_id` (uuid, optional) — base city if omitted
- `feature_type` (string, optional) — e.g. `block`, `ageb`
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
| `REIMAGINA_API_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8000` | Comma-separated origins allowed through CORS. |
| `REIMAGINA_API_HOST` | `127.0.0.1` | uvicorn bind host. |
| `REIMAGINA_API_PORT` | `8000` | uvicorn bind port. |
| `REIMAGINA_API_LOG_LEVEL` | `info` | uvicorn log level. |

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
curl -s http://localhost:8000/health/ready
# {"status":"ok","db":"reachable"}

curl -s http://localhost:8000/indicators | jq '.[].slug'

curl -s 'http://localhost:8000/indicators/urban_form_block_building_density/values?limit=5' | jq '.[].value_numeric'

curl -s 'http://localhost:8000/indicators/urban_form_block_building_density/values.csv?feature_type=ageb' | head

curl -s 'http://localhost:8000/indicators/urban_form_block_building_density/values.parquet?feature_type=ageb' \
  > /tmp/density.parquet
python -c "import geopandas; print(geopandas.read_parquet('/tmp/density.parquet').head())"
```
