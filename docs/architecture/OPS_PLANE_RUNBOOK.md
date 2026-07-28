# Ops-plane runbook (Hatchet)

For whoever is holding the pager, and for anyone who wants to *see* a run rather
than query one. The counterpart to Track A's runbook, which is deliberately all
SQL — that difference is the point.

## What is running

Unlike the library track, there is an engine, and it must be up.

| process | required | why |
|---|---|---|
| engine (`hatchet-lite`) | **yes** | schedules everything; nothing runs without it |
| worker (`python -m apps.worker`) | **yes** | there is no in-process mode |
| API (`apps/api`) | for HTTP | submits and reads; does not execute units |

⚠ **A worker is not an optimisation here, it is a precondition.** `run_no_wait`
returns a workflow-run id, but `runs.get()` on it returns **404** until a worker
has registered the workflow definition. A run id can exist and be unqueryable.
The library track has no equivalent state.

```bash
docker compose -f infra/docker/docker-compose.hatchet.yml up -d
```

Postgres is **not** in that compose file — the engine uses the host instance,
the same one holding domain data, in its own `hatchet` database. Deliberate: it
keeps the comparison honest by not charging this track for a database the other
track also needs. ⚠ That database's timezone must be UTC or the engine refuses
to start.

## Testing it in a browser

This is the capability Track A does not have at any price, so it is worth
knowing what is actually there.

**http://localhost:8888** — dashboard and REST API on the same port. Log in with
the seeded credentials, which are defaults in the engine's own source
(`pkg/config/database/config.go:59-60`), not something we chose:

```
admin@example.com
Admin123!!
```

⚠ Development defaults, hardcoded and public. Change them before this is
reachable from anywhere but localhost.

What is worth clicking, in the order it answers questions:

| view | what it answers | Track A equivalent |
|---|---|---|
| **Runs** list | which runs exist, status, duration | `SELECT` on `orchestration_run` |
| filter by metadata | "every run for scenario X" — key `scenario_id` | `WHERE scenario_id = …` |
| a run's **DAG view** | which indicators ran, which skipped, what is blocked on what | nothing — the shape is implicit in `wave.py` |
| a task's **Logs** tab | `ctx.log()` output, per unit, searchable | process stdout |
| a task's **Input/Output** | the exact `RunInput` and what came back | `job_unit` row |
| **Replay** button | re-run from the failure, no resubmit | `fork_workflow`, CLI only |
| **Workers** | is a worker connected, how many slots free | `ps` |

⚠ **The DAG view renders a skipped unit the same green as a computed one.** The
engine's status enum has no `skipped` (spike B7), so a non-dirty indicator shows
COMPLETED. To tell them apart, open the run's **output** — the finalizer writes a
per-unit verdict there (spike B8):

```json
{"overall": "partial",
 "units": {"urban_form_block_compactness": {"status": "succeeded"},
           "urban_form_block_planned_floors": {"status": "skipped"}}}
```

That is also what `GET /jobs/{run_id}` returns, so the API and the dashboard do
not disagree — but the dashboard's *status chip* and the run's *verdict* can,
and the verdict is the one to believe.

### Driving a run from the browser

The dashboard triggers runs directly: **Workflows → `reimagina-run` → Trigger
Run**, then paste a `RunInput`:

```json
{"run_id": "", "dirty": ["urban_form_block_compactness"],
 "features": {"urban_form_block_compactness": ["<feature_id>"]},
 "scenario_id": null, "aoi_id": "cuauhtemoc",
 "code_version": "dev", "concurrency_key": "baseline"}
```

⚠ `dirty` is computed by the submitter, not by the workflow — trigger it by hand
and you are asserting the dirty set yourself. Useful for testing one unit,
wrong for anything real. Note also that `concurrency_key` collides with live
runs: `max_runs=1` with `CANCEL_NEWEST` means a hand-triggered run sharing a key
with an in-flight one **is cancelled**, not queued.

### If the dashboard will not load

`/api/v1/meta` answers unauthenticated and is the fastest check that the engine
is serving at all:

```bash
curl -s http://localhost:8888/api/v1/meta
```

Expect `{"auth":{"schemes":["basic"]},...}`. ⚠ `/api/v1/readiness` and
`/api/v1/liveness` return **403**, not 200 — they are not anonymous health
checks, so do not wire a probe to them expecting otherwise.

Two failure modes seen more than once:

- **Login succeeds then immediately bounces** — `SERVER_AUTH_COOKIE_DOMAIN` is
  unset or does not match the host you are browsing.
- **Engine will not start** — the `hatchet` database's timezone is not UTC.

## Is a run stuck?

One plane, not two. There is no table of ours to cross-check against, which
is faster when the engine is healthy and leaves nothing when it is not.

```bash
# needs HATCHET_CLIENT_TOKEN
hatchet-sdk equivalent: hatchet.runs.list(statuses=[RUNNING], since=…)
```

In the dashboard: **Runs**, filter status `Running`, sort by duration. Anything
older than `REIMAGINA_UNIT_TIMEOUT_MIN` should have been killed already; if it
has not, the worker is not reporting.

| symptom | meaning |
|---|---|
| run QUEUED, no worker in **Workers** | nothing is registered; start the worker |
| run QUEUED, workers present, slots full | saturation — check `schedule_timeout` headroom |
| task RUNNING far past its timeout | worker lost the engine but kept the statement; see below |
| run FAILED, verdict says `partial` | some units succeeded; do **not** blanket-retry |

## A unit that will not stop

Unchanged from the other branch, because neither ops-plane solves it:
cancellation is cooperative and nothing interrupts a blocking psycopg call
(spike B4). The engine marks the task CANCELLED immediately while the SQL keeps
running.

```sql
SELECT jr.id, jr.backend_pid, jr.status, a.state, a.query_start
FROM jobs_runtime.job_run jr
LEFT JOIN pg_stat_activity a ON a.pid = jr.backend_pid
WHERE jr.run_id = :run_id AND jr.status NOT IN ('succeeded','failed','cancelled');
```

`pg_cancel_backend(backend_pid)` is what actually ends it. `job_run` survives on
every branch precisely because of this — the answer lives outside both engines.

## Configuration that will bite you

| variable | unset behaviour | why it matters |
|---|---|---|
| `HATCHET_CLIENT_TOKEN` | **import fails** | `hatchet_app` constructs `Hatchet()` at module scope, so even unit tests need it |
| `DATABASE_URL_DIRECT` | unit runners fail | the engine's DSN is separate from the domain one |
| `REIMAGINA_UNIT_TIMEOUT_MIN` | 120 | ⚠ Hatchet's own default is **60 seconds** — it would kill a healthy unit a minute in |
| `REIMAGINA_QUEUE_TIMEOUT_HOURS` | 6 | ⚠ Hatchet's default is **5 minutes**, and timed-out queued work reports FAILED — indistinguishable from broken compute |
| `REIMAGINA_UNIT_RETRIES` | 2 | with backoff; `NonRetryableException` opts out deterministic failures |
| `REIMAGINA_DEDUP_TTL_S` | 60 | idempotency window — a debounce, not a result cache |
| `REIMAGINA_RECOMPUTE_CRON` | no cron | set to e.g. `0 3 * * *` for a nightly baseline |

⚠ `SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD` defaults to **720h — runs
older than 30 days are deleted.** On this track that is the run history, since
there is no table of ours. Track A's history lives in `orchestration_run` and is
bounded only by disk.

## What is not ready

Stated plainly so nobody discovers it at 3am:

- **No load test**, never run multi-worker beyond a spike, never deployed.
- **`rate_limits` and `slot_cost` are not wired up.** They are the engine-native
  answer to N workers saturating one Postgres, and unmeasured.
- **`ctx.refresh_timeout` is not wired up**, so a wedged unit burns the full
  `REIMAGINA_UNIT_TIMEOUT_MIN` rather than dying in minutes. Needs a runner
  progress callback that does not exist yet.
- **Adding an indicator requires a redeploy**, not just a catalog row — the DAG
  is declared at import. Track A reads the registry at runtime.
- **Metrics are available and off.** `SERVER_OBSERVABILITY_ENABLED` and
  `SERVER_PROMETHEUS_ENABLED` both default false; `/api/v1/meta` confirms it.
  Turning them on is a config change, not code — the library track has no
  equivalent to turn on.
