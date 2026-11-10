#!/usr/bin/env bash
#
# Build the Mexicali pilot from real data, on a machine that has the lake mirror.
#
# The sibling of scripts/acceptance_run.sh, and the opposite trade: acceptance
# generates a synthetic territory and needs no network and no credentials, so a
# reviewer can run it anywhere. This one needs the Azure lake mirror on disk and
# outbound network, and in exchange every number it produces is about a real
# city.
#
# What it needs:
#   - PostgreSQL with PostGIS and pgRouting
#   - data/sources/azure/inegi/marco_geostadistico/2024/  (INEGI blocks + AGEBs)
#   - data/sources/azure/inegi/censo/                     (2020 census)
#   - data/sources/azure/exe/002_mexicali/                (IMIP amenities)
#   - network access to Overture's S3 and to Overpass
#
# ⚠ The mirror is NOT in the repository. `data/` is gitignored — "anything
# under data/ is local cache or bootstrap output, never tracked" — so a fresh
# clone has none of it and step 0 will say so rather than failing obscurely
# five minutes in.
#
# Usage:
#   scripts/mexicali_run.sh                 # drops and rebuilds $DATABASE_URL
#   scripts/mexicali_run.sh --keep          # reuse the existing database
#   scripts/mexicali_run.sh --skip-network  # no Overpass; skips accessibility
#
# DATABASE_URL comes from .env. DATABASE_URL_ADMIN is derived from it when
# unset (same server, `postgres` database); set it explicitly on a managed
# server where the connecting role may not create databases.
#
# ⚠ Reusing one database is fine *without* --keep, because step 1a drops and
# recreates it: the synthetic acceptance territory cannot survive into a real
# run. With --keep it can, and that matters — the synthetic territory sits at
# real Mexicali coordinates (-115.469, 32.624) and carries no CVE_ENT, so the
# r040 membership clause decides it spatially and its 16 invented blocks join
# the real municipal AOI. They would then be averaged into compactness and
# building density alongside the real INEGI ones, with nothing erroring.
# `scripts/acceptance_run.sh` rebuilds its own territory from nothing, so
# wiping it here loses nothing reproducible.

set -euo pipefail

KEEP=0
SKIP_NETWORK=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --skip-network) SKIP_NETWORK=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-uv run python}"
CLI="$PY -m apps.cli.app"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${DATABASE_URL:?set DATABASE_URL (see .env.example)}"

# DATABASE_URL_ADMIN is a DSN that may CREATE DATABASE, needed because step 1a
# drops and recreates the target and cannot do that while connected to it.
#
# Derived rather than required. `.env` carries DATABASE_URL and
# DATABASE_URL_DIRECT but not this one, so the script refused to start over a
# variable whose value is mechanical: the same server, the `postgres`
# maintenance database. Asking for it bought nothing and cost a failed run.
#
# Overridable, because the derivation assumes the connecting role may create
# databases on that server — true for a local Homebrew Postgres, not
# necessarily for a managed one.
if [[ -z "${DATABASE_URL_ADMIN:-}" ]]; then
  DATABASE_URL_ADMIN="${DATABASE_URL%/*}/postgres"
  export DATABASE_URL_ADMIN
  printf 'DATABASE_URL_ADMIN not set; using %s\n' "$DATABASE_URL_ADMIN"
fi

step() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# INEGI codes are zero-padded strings: "02" is Baja California, not 2, and the
# parquet is Hive-partitioned on the padded value.
CVE_ENT=02
CVE_MUN=002
AOI_NAME="Mexicali"
PROJECT_NAME="Mexicali Pilot"

# The urban-core box, matching reimagina.pipeline.overture.MEXICALI_BBOX. Used
# as the OSM fetch boundary in step 7; the Overture extract in step 6 takes the
# same box as its own default, so the two layers cover the same ground.
MEXICALI_BBOX="-115.70,32.40,-115.20,32.80"

# Set if step 7's Overpass fetch fails; reported at the end so a degraded run
# cannot be mistaken for a complete one.
NETWORK_FAILED=0

# ── 0. Preflight ─────────────────────────────────────────────────────────────
#
# Named individually. "the mirror is missing" sends someone to re-download 17 GB
# when one 52 KB file is absent.
step "0. Checking the lake mirror and the toolchain"
MISSING=0
check_path() {
  if [[ -e "$1" ]]; then
    note "ok       $1"
  else
    note "MISSING  $1  ($2)"
    MISSING=1
  fi
}
check_path data/sources/azure/inegi/marco_geostadistico/2024 \
  "INEGI blocks and AGEBs — without these there are no analysis units and no indicator can run"
check_path data/sources/azure/inegi/censo \
  "2020 census — without it the edificability family has no population"
check_path data/sources/azure/exe/002_mexicali/equipamientos.geoparquet \
  "IMIP amenities — without them the accessibility family has no destinations"
if (( MISSING )); then
  cat >&2 <<'EOF'

One or more lake paths are absent. `data/` is gitignored, so a fresh clone
never has them; they come from the Azure container recorded in
docs/AZURE_DATA_INVENTORY.md, pulled with Storage Explorer.

What still works without the mirror: scripts/acceptance_run.sh (fully
synthetic), and the Overture half of this script — `overture divisions`,
`overture aoi-from-division`, `overture extract`, `overture load` — which
gives a real boundary and real buildings but no census blocks, and therefore
no indicator.
EOF
  exit 1
fi

$CLI data verify-sources || note "verify-sources reported problems; continuing, since the paths above are what this script reads"

# ── 1. Schema ────────────────────────────────────────────────────────────────
DB_NAME="${DATABASE_URL##*/}"
DB_NAME="${DB_NAME%%\?*}"

if [[ $KEEP -eq 0 ]]; then
  step "1a. Dropping and recreating ${DB_NAME}"
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

step "1b. Migrating and seeding the catalogs"
$CLI bootstrap-db
$CLI verify-schema
$CLI feat seed-types
$CLI indicators seed-catalog

# ── 2. INEGI geometry ────────────────────────────────────────────────────────
step "2. Loading INEGI Marco Geoestadístico for Baja California (CVE_ENT=${CVE_ENT})"
note "filtering a ~3 GB national parquet; geometry is WKB in a BLOB column, not GeoParquet"
$CLI inegi-marco bootstrap \
  --cve-ent "$CVE_ENT" \
  --layers areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas

# ── 3. The AOI ───────────────────────────────────────────────────────────────
#
# The INEGI-derived constructor, which declares a membership_key. Unlike
# `overture aoi-from-division`, this one can: every block and AGEB from step 2
# carries CVE_ENT and CVE_MUN, so membership is identifier-based rather than
# geometric, which is what r040 is for — a block belongs to Mexicali because
# INEGI says so, not because its polygon overlaps.
step "3. Creating the Mexicali AOI from the INEGI municipality polygon"
AOI_OUT="$($CLI cdmx aoi-from-alcaldia \
  --cve-ent "$CVE_ENT" --cve-mun "$CVE_MUN" \
  --name "$AOI_NAME" --project-name "$PROJECT_NAME")"
echo "$AOI_OUT"
AOI_ID="$($PY -c 'import json,sys; print(json.load(sys.stdin)["aoi_id"])' <<<"$AOI_OUT")"
[[ -n "$AOI_ID" ]] || { echo "could not read aoi_id" >&2; exit 1; }
export AOI_ID
note "AOI_ID=$AOI_ID"

# ── 4. Attributes and points ─────────────────────────────────────────────────
step "4. Loading the 2020 census as block observations"
$CLI inegi-censo bootstrap --cve-ent "$CVE_ENT"

step "5. Loading Mexicali amenities (IMIP / DENUE)"
$CLI points bootstrap --layers mexicali_amenities

# ── 6. Buildings, from Overture ──────────────────────────────────────────────
#
# The lake has no Mexicali buildings. Overture does, and its divisions theme is
# also where a boundary comes from on a machine with no mirror — see
# docs/superpowers/specs/MEXICALI_FROM_OVERTURE.md. Here the boundary is
# INEGI's, so only the buildings are fetched.
step "6. Extracting and loading Overture buildings"
BUILDINGS=data/overture_buildings_mexicali.parquet
if [[ -f "$BUILDINGS" ]]; then
  note "reusing $BUILDINGS"
else
  note "~387k rows in the default MEXICALI_BBOX; peak RSS measured at 4.4 GB"
  $CLI overture extract --output-path "$BUILDINGS"
fi
$CLI overture load \
  --raw-path "$BUILDINGS" \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id "$AOI_ID"

# ── 7. The street network ────────────────────────────────────────────────────
if (( SKIP_NETWORK )); then
  step "7. Skipping the OSM network (--skip-network)"
  note "the accessibility family needs curated_geo.road_edge and will be reported unavailable"
else
  step "7. Fetching the OSM pedestrian network and publishing it as roads"
  note "needs Overpass. Also what gives the map its street context, with ODbL attribution"
  # ⚠ --clip-bbox, not the bare AOI. The INEGI municipality polygon is
  # 9,971 km2 reaching San Felipe, almost all desert; a walking graph over it
  # is a multi-gigabyte Overpass request whose result is ~99% empty, and a
  # truncated fetch looks exactly like a city with no streets. The clip
  # narrows the fetch only — membership, overlaps and every indicator still
  # scope to the full municipal boundary.
  #
  # ⚠ Non-fatal on purpose, and this is the one `set -e` exception in the
  # script. Overpass is a free public service that rate-limits and times out;
  # with `set -e` a transient 429 here would abort the run and discard steps
  # 2-6 — the ~3 GB INEGI filter, the census join, the 387k-row Overture load.
  # The accessibility family is already in ATTEMPTED rather than REQUIRED, so
  # a missing graph degrades the run instead of failing it, and step 8 still
  # has to execute or every tile comes back 204.
  #
  # Re-run just this step afterwards; it replaces the whole network anyway.
  if ! $CLI network bootstrap --aoi-id "$AOI_ID" --clip-bbox "$MEXICALI_BBOX"; then
    NETWORK_FAILED=1
    note "the OSM fetch failed — continuing without a street network"
    note "retry on its own: $CLI network bootstrap --aoi-id $AOI_ID --clip-bbox $MEXICALI_BBOX"
  fi
fi

# ── 8. Membership, which has to be last ──────────────────────────────────────
#
# ⚠ Order is load-bearing, and getting it wrong fails silently. `get_tile`
# scopes by AOI through an EXISTS against aoi_feature, so a feature loaded
# AFTER this pass is correctly typed, present in serving_geo, and returns 204
# on every tile with no error anywhere. That is exactly how the street layer
# broke once: 40 road features, all present, all invisible.
step "8. Computing AOI membership — after every feature exists"
$CLI aoi compute-features --aoi-id "$AOI_ID"

# ── 9. Overlaps ──────────────────────────────────────────────────────────────
step "9. Declaring and computing feature overlaps"
$CLI overlap declare-pair \
  --source-context global --source-slug building \
  --target-context MEX --target-slug block
$CLI overlap declare-pair \
  --source-context MEX --source-slug block \
  --target-context MEX --target-slug ageb
$CLI overlap list-pairs
$CLI overlap compute-all --aoi-id "$AOI_ID"

# ── 10. Indicators ───────────────────────────────────────────────────────────
#
# ⚠ Unlike acceptance_run.sh, this does NOT require all eleven to produce rows,
# and the difference is not laziness. The synthetic territory is built to
# satisfy every indicator's inputs; a real city is not. `planned_floors` reads
# a `floors` attribute and the edificability family reads `num_levels`,
# `unit_size` and `people_per_household` — scenario inputs that no INEGI or
# Overture layer carries. Demanding rows from those would mean inventing the
# attributes, which is worse than reporting them unavailable.
#
# So: REQUIRED must produce rows, because their inputs were just loaded.
# ATTEMPTED are tried and reported either way. A failure there is information
# about the data, not a broken run.
step "10. Computing indicators"

REQUIRED=(
  urban_form_block_compactness
  urban_form_block_building_density
  urban_form_block_composite
)
ATTEMPTED=(
  urban_form_block_planned_floors
  edificability_block_housing_units
  edificability_block_population
  edificability_block_density
  accessibility_block_school_network_m
  accessibility_block_health_network_m
  accessibility_block_park_network_m
  livability_block_score
)

rows_for() {
  $CLI indicators compute --slug "$1" --aoi-id "$AOI_ID" 2>/dev/null \
    | tr -d '\n' | grep -oE '"rows_written": *[0-9]+' | grep -oE '[0-9]+$' || true
}

for slug in "${REQUIRED[@]}"; do
  rows="$(rows_for "$slug")"
  printf '  %-44s %s\n' "$slug" "rows_written=${rows:-NONE REPORTED}"
  # Parsed and compared, not grepped for non-emptiness: `"rows_written": 0` is
  # a non-empty match, so a grep-only check passes an indicator that computed
  # nothing — the one thing this step exists to catch.
  if [[ -z "$rows" ]]; then
    echo "  ^ required indicator reported no rows_written at all" >&2
    exit 1
  fi
  if (( rows == 0 )); then
    echo "  ^ required indicator wrote 0 rows — its inputs loaded but nothing computed" >&2
    exit 1
  fi
done

SKIPPED=()
for slug in "${ATTEMPTED[@]}"; do
  rows="$(rows_for "$slug")"
  if [[ -n "$rows" && "$rows" -gt 0 ]]; then
    printf '  %-44s %s\n' "$slug" "rows_written=$rows"
  else
    printf '  %-44s %s\n' "$slug" "unavailable — inputs absent on real data"
    SKIPPED+=("$slug")
  fi
done

# ── 11. Inspection ───────────────────────────────────────────────────────────
step "11. What is in the database"
$CLI explore feature-stats
$CLI explore serving-stats
$CLI explore sources

step "Done"
note "AOI_ID=$AOI_ID"
if (( NETWORK_FAILED )); then
  note "⚠ INCOMPLETE: the OSM network fetch failed, so curated_geo.road_edge is"
  note "  empty — the map has no street context and the accessibility family"
  note "  could not run. Everything else above is real."
fi
if (( ${#SKIPPED[@]} )); then
  note "unavailable on real data (${#SKIPPED[@]}): ${SKIPPED[*]}"
  note "each needs an attribute no INEGI or Overture layer carries; see step 10's comment"
fi
note "map: REIMAGINA_APP_DSN=$DATABASE_URL $PY -m apps.api  +  (cd apps/web && npm run dev)"
