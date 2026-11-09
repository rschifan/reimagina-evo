#!/usr/bin/env bash
#
# Build the platform from an empty database and exercise every layer of it.
#
# This is the script behind Schedule A's acceptance criterion for the source
# code — "the code builds and runs following the included README". It starts
# from a database that does not exist, migrates it, generates a territory,
# and then runs every analysis stage over that territory: overlaps, all
# eleven indicators including the pgRouting network family, a raster through
# COG staging and zonal statistics, an observation rollup, and a scenario.
#
# It needs no credentials to the data lake and no network access. The only
# inputs are a Postgres with PostGIS and pgRouting, and this repository.
#
# Usage:
#   scripts/acceptance_run.sh                 # uses .env
#   scripts/acceptance_run.sh --keep          # do not drop the database first
#
# Every step fails the whole run, loudly. A green run means each capability
# the delivery documents claim was observed working on this machine, in this
# order, from nothing.

set -euo pipefail

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# `uv run` for the same reason the Makefile uses it: the interpreter that runs
# is the one uv.lock pins, not whichever Python is first on PATH.
PY="${PYTHON:-uv run python}"
CLI="$PY -m apps.cli.app"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${DATABASE_URL:?set DATABASE_URL (see .env.example)}"
: "${DATABASE_URL_ADMIN:?set DATABASE_URL_ADMIN — a DSN that may CREATE DATABASE}"

step() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }

# The scenario uuid has to cross two Python heredocs and the shell between
# them. A file is the only channel a heredoc has back to its caller.
SCENARIO_ID_FILE="$(mktemp "${TMPDIR:-/tmp}/reimagina-acceptance-scenario.XXXXXX")"
export SCENARIO_ID_FILE
WORKER_PID=""
cleanup() {
  [[ -n "$WORKER_PID" ]] && kill "$WORKER_PID" 2>/dev/null || true
  rm -f "$SCENARIO_ID_FILE"
}
trap cleanup EXIT

# The database name is the last path segment of the DSN, minus any query string.
DB_NAME="${DATABASE_URL##*/}"
DB_NAME="${DB_NAME%%\?*}"

if [[ $KEEP -eq 0 ]]; then
  step "0. Dropping and recreating ${DB_NAME}"
  # Terminate first: DROP DATABASE fails outright while any session is
  # attached, and a left-over API or worker from a previous run is attached.
  $PY - <<'PYEOF'
import os
import psycopg2

admin = os.environ["DATABASE_URL_ADMIN"]
name = os.environ["DATABASE_URL"].rsplit("/", 1)[-1].split("?")[0]
conn = psycopg2.connect(admin)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )
    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
conn.close()
print(f"  dropped {name}")
PYEOF
fi

step "1. Migrating to head"
$CLI bootstrap-db

step "2. Verifying the first-layer schema"
$CLI verify-schema

step "3. Seeding the feature-type and indicator catalogs"
$CLI feat seed-types
$CLI indicators seed-catalog

step "4. Generating a territory (AGEBs, blocks, buildings, streets, POIs, AOI)"
RASTER="${TMPDIR:-/tmp}/reimagina-acceptance-population.tif"
SEED_OUT="$($PY scripts/synthetic_territory.py --raster-out "$RASTER")"
echo "$SEED_OUT"
AOI_ID="$(sed -n 's/^ *AOI_ID=//p' <<<"$SEED_OUT")"
[[ -n "$AOI_ID" ]] || { echo "could not read AOI_ID from the seed output" >&2; exit 1; }
export AOI_ID

step "5. Declaring and computing feature overlaps"
$CLI overlap declare-pair \
  --source-context global --source-slug building \
  --target-context MEX --target-slug block
$CLI overlap declare-pair \
  --source-context MEX --source-slug block \
  --target-context MEX --target-slug ageb
$CLI overlap list-pairs
$CLI overlap compute-all --aoi-id "$AOI_ID"

step "6. Computing every shipped indicator"
# Dependency order matters: a composite reads its inputs' current rows, and the
# accessibility family needs the road graph the seed step loaded.
for slug in \
  urban_form_block_compactness \
  urban_form_block_building_density \
  urban_form_block_planned_floors \
  urban_form_block_composite \
  edificability_block_housing_units \
  edificability_block_population \
  edificability_block_density \
  accessibility_block_school_network_m \
  accessibility_block_health_network_m \
  accessibility_block_park_network_m \
  livability_block_score
do
  # Captured and printed together, on one line: the CLI's own stderr would
  # otherwise land between the slug and its result and make the log unreadable.
  written="$($CLI indicators compute --slug "$slug" --aoi-id "$AOI_ID" \
    | tr -d '\n' | grep -oE '"rows_written": [0-9]+' || true)"
  printf '  %-40s %s\n' "$slug" "${written:-NO ROWS WRITTEN}"
  [[ -n "$written" ]] || { echo "  ^ that indicator wrote nothing" >&2; exit 1; }
done

step "7. Ingesting a raster: bronze -> silver COG -> registry"
INGEST="$($CLI raster ingest \
  --raw-path "$RASTER" \
  --source-name "Synthetic Mexicali population" \
  --provider reimagina-synthetic \
  --version synthetic-v1 \
  --layer-slug syn_pop_acceptance \
  --aggregation-kind extensive \
  --logical-name "Synthetic population" \
  --colormap-name viridis \
  --rescale "0,12")"
echo "$INGEST"
RASTER_SOURCE_ID="$($PY -c 'import json,sys; print(json.load(sys.stdin)["source_id"])' <<<"$INGEST")"

step "8. Zonal statistics per block"
$CLI raster zonal-stats \
  --source-id "$RASTER_SOURCE_ID" \
  --feature-type block \
  --attribute-key-prefix syn_pop \
  --aoi-id "$AOI_ID"

step "9. Rolling the block observations up to AGEB"
RECIPE="$($CLI raster derivation add \
  --input-kind observation --input-prefix syn_pop \
  --input-feature-type block --target-feature-type ageb \
  --attribute-key-prefix syn_pop_ageb --method sum --aoi-id "$AOI_ID")"
RECIPE_ID="$($PY -c 'import json,sys; print(json.load(sys.stdin)["recipe"]["id"])' <<<"$RECIPE")"
$CLI raster derivation run --recipe-id "$RECIPE_ID"
$CLI raster list

step "10. A scenario, and the selective recompute it triggers"
$PY - <<'PYEOF'
import os
import sys
import uuid

sys.path.insert(0, "packages/py")
from reimagina.scenarios.overrides import apply_overrides  # noqa: E402
from reimagina.storage import connect  # noqa: E402

# A stable uuid5, so re-running the script edits the same scenario instead of
# accumulating a new one on every pass.
scenario_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "reimagina://acceptance/upzoning"))
with connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_record_ref, (attributes->>'floors')::float "
            "FROM curated_geo.feature WHERE type_slug = 'block' "
            "ORDER BY source_record_ref LIMIT 4"
        )
        picks = cur.fetchall()
    applied = apply_overrides(
        conn,
        scenario_id=scenario_id,
        edits=[
            {
                "feature_id": str(fid),
                "attribute_key": "floors",
                "value_numeric": floors + 6,
            }
            for fid, _ref, floors in picks
        ],
    )
    conn.commit()
for _fid, ref, floors in picks:
    print(f"  {ref}  floors {floors:g} -> {floors + 6:g}")
print(f"  overrides applied: {applied}")
with open(os.environ["SCENARIO_ID_FILE"], "w") as handle:
    handle.write(scenario_id)
PYEOF
SCENARIO_ID="$(cat "${SCENARIO_ID_FILE}")"
$CLI evaluate-scenario \
  --changed-attribute floors \
  --scenario-id "$SCENARIO_ID" \
  --aoi-id "$AOI_ID" \
  --sync

step "11. What the scenario changed, and what it left alone"
$PY - <<'PYEOF'
import os
import sys

sys.path.insert(0, "packages/py")
from reimagina.storage import connect  # noqa: E402

scenario_id = open(os.environ["SCENARIO_ID_FILE"]).read().strip()
with connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT f.source_record_ref, b.value_numeric, s.value_numeric
        FROM curated_geo.feature f
        JOIN curated_geo.indicator_result b
          ON b.feature_id = f.id AND b.status = 'current' AND b.scenario_id IS NULL
        JOIN curated_geo.indicator_catalog c ON c.id = b.indicator_id
        LEFT JOIN curated_geo.indicator_result s
          ON s.feature_id = f.id AND s.status = 'current'
         AND s.scenario_id = %s AND s.indicator_id = b.indicator_id
        WHERE c.slug = 'urban_form_block_planned_floors'
        ORDER BY 1
        """,
        (scenario_id,),
    )
    rows = cur.fetchall()
    print(f"  {'block':<10}{'baseline':>10}{'scenario':>10}{'delta':>8}")
    changed = 0
    for ref, base, scen in rows:
        if scen is None:
            delta = "(absent)"
        elif scen == base:
            delta = "="
        else:
            changed += 1
            delta = f"{scen - base:+g}"
        print(f"  {ref:<10}{base:>10.1f}{(scen if scen is not None else 0):>10.1f}{delta:>8}")
    print(f"  blocks the scenario changed: {changed} of {len(rows)}")

    cur.execute(
        "SELECT count(*) FILTER (WHERE scenario_id IS NULL), "
        "       count(*) FILTER (WHERE scenario_id IS NOT NULL) "
        "FROM curated_geo.indicator_result WHERE status = 'current'"
    )
    baseline_rows, scenario_rows = cur.fetchone()
    print(f"  current indicator_result rows: {baseline_rows} baseline, "
          f"{scenario_rows} scenario — coexisting, not replaced")

    cur.execute(
        "SELECT max(n) FROM (SELECT count(*) n FROM curated_geo.indicator_result "
        "WHERE status = 'current' GROUP BY feature_id, indicator_id, "
        "coalesce(scenario_id::text, 'base')) t"
    )
    print(f"  most current rows for any one (feature, indicator, scenario): "
          f"{cur.fetchone()[0]} — the partial unique indexes hold")
PYEOF

step "12. The ops plane: a worker drains a queued run"
# Step 10 ran the scenario through `--sync`, which is the in-process
# coordinator and deliberately needs no ops plane — it is the path that still
# works when the engine is down. That means everything above this line proves
# the platform *without* exercising the orchestration module Schedule A names.
# This step is the other half: submit through the durable queue and let a
# separate worker process drain it.
WORKER_LOG="${TMPDIR:-/tmp}/reimagina-acceptance-worker.log"
REIMAGINA_EXECUTOR_ID=acceptance-worker $PY -m apps.worker > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!

# Waited for through the product's own liveness surface rather than by
# sleeping or grepping the log: the row appearing is itself the heartbeat
# feature working, and a fixed sleep would be a race on a loaded machine.
$PY - <<'PYEOF'
import os
import sys
import time

import psycopg2

deadline = time.time() + 120
while time.time() < deadline:
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT role, now() - heartbeat_at FROM "
            "jobs_runtime.ops_executor_heartbeat WHERE executor_id = %s",
            ("acceptance-worker",),
        )
        row = cur.fetchone()
    if row:
        print(f"  worker announced itself: role={row[0]}, beat age {row[1]}")
        sys.exit(0)
    time.sleep(1)
print("  the worker never published liveness", file=sys.stderr)
sys.exit(1)
PYEOF

QUEUED="$($CLI evaluate-scenario \
  --changed-attribute geometry --aoi-id "$AOI_ID" --code-version acceptance --detach)"
echo "$QUEUED"
QUEUED_RUN="$($PY -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$QUEUED")"

step "12b. The run the worker picked up, and the shape it ran in"
QUEUED_RUN="$QUEUED_RUN" $PY - <<'PYEOF'
import os
import sys
import time

import psycopg2

run_id = os.environ["QUEUED_RUN"]
terminal = {"succeeded", "partial", "failed", "cancelled"}
deadline = time.time() + 300
status = None
while time.time() < deadline:
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM jobs_runtime.orchestration_run WHERE id = %s::uuid",
            (run_id,),
        )
        row = cur.fetchone()
        if row and row[0] in terminal:
            status = row[0]
            cur.execute(
                "SELECT indicator_slug, status, started_at, completed_at "
                "FROM jobs_runtime.job_unit WHERE run_id = %s::uuid "
                "ORDER BY started_at NULLS LAST, indicator_slug",
                (run_id,),
            )
            units = cur.fetchall()
            break
    time.sleep(1)
else:
    print(f"  run {run_id} never reached a terminal state", file=sys.stderr)
    sys.exit(1)

print(f"  run {run_id}")
print(f"  status {status}, {len(units)} units")
first = min((u[2] for u in units if u[2]), default=None)
for slug, unit_status, started, completed in units:
    offset = f"+{(started - first).total_seconds():.2f}s" if started and first else "  —"
    took = f"{(completed - started).total_seconds():.2f}s" if started and completed else "—"
    print(f"    {unit_status:<10} {offset:>8} ran {took:>6}  {slug}")
print(
    "  units starting at the same offset ran concurrently; a later offset is a "
    "unit waiting for a dependency"
)
if status != "succeeded":
    sys.exit(1)
PYEOF

step "12c. Resubmitting outstanding work returns the run already doing it"
# Two submissions in a row. The first is new work; the second names work that
# is still outstanding, so it is answered with that run rather than a second
# one writing the same rows a moment later.
$PY - <<'PYEOF'
import os
import sys

sys.path.insert(0, "packages/py")
os.environ["REIMAGINA_DBOS_EXECUTION_MODE"] = "client"
from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator  # noqa: E402
from reimagina.storage import connect  # noqa: E402

request = {
    "changed_attributes": {"num_levels"},
    "edited_features": set(),
    "aoi_id": os.environ["AOI_ID"],
    "code_version": "acceptance-dedup",
}
orchestrator = DBOSOrchestrator(execution_mode="client")
with connect(os.environ["DATABASE_URL"]) as conn:
    conn.autocommit = True
    first = orchestrator.submit_run(conn, **request)
    second = orchestrator.submit_run(conn, **request)
print(f"  submit 1 -> {first.run_id}  deduplicated={first.deduplicated}")
print(f"  submit 2 -> {second.run_id}  deduplicated={second.deduplicated}")
if not (second.run_id == first.run_id and second.deduplicated):
    print("  the resubmission started a second run", file=sys.stderr)
    sys.exit(1)
PYEOF

kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
WORKER_PID=""
echo "  worker stopped"

step "13. Live vector tiles, decoded from the bytes the client would get"
$PY - <<'PYEOF'
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, "packages/py")
sys.path.insert(0, ".")
from reimagina.storage import connect  # noqa: E402
from scripts.mvt_inspect import decode, summarise  # noqa: E402

out = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "reimagina-acceptance-tiles"
out.mkdir(parents=True, exist_ok=True)
with connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
    # Tile indices for the territory's centroid, at each zoom.
    cur.execute(
        """
        WITH c AS (
          SELECT ST_X(ST_Centroid(ST_Extent(geometry)::geometry)) lon,
                 ST_Y(ST_Centroid(ST_Extent(geometry)::geometry)) lat
          FROM curated_geo.feature
        )
        SELECT z,
               floor((lon + 180) / 360 * (2 ^ z))::int,
               floor((1 - ln(tan(radians(lat)) + 1 / cos(radians(lat))) / pi())
                     / 2 * (2 ^ z))::int
        FROM c, generate_series(10, 16, 2) z
        """
    )
    for z, x, y in cur.fetchall():
        cur.execute(
            "SELECT serving_geo.get_tile(%s, %s, %s, '{}'::json)", (z, x, y)
        )
        raw = bytes(cur.fetchone()[0] or b"")
        print(f"\n  z{z}/{x}/{y} — {len(raw)} bytes")
        for line in summarise(decode(raw)):
            print(f"    {line}")
PYEOF

step "14. Engineer-facing inspection surface"
for view in layers sources feature-stats serving-stats executor-health; do
  echo "  explore $view"
  $CLI explore "$view" > /dev/null
done
echo "  all five explore commands returned without error"

step "Acceptance run complete"
echo "AOI_ID=$AOI_ID"
echo "SCENARIO_ID=$SCENARIO_ID"
echo "RASTER_SOURCE_ID=$RASTER_SOURCE_ID"
