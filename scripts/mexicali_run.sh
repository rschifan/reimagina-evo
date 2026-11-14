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
#   scripts/mexicali_run.sh --low-memory    # slow, bounded-memory extract
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
LOW_MEMORY=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --skip-network) SKIP_NETWORK=1 ;;
    --low-memory) LOW_MEMORY=1 ;;
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

# Set when an optional step fails; all reported at the end, so a degraded run
# cannot be mistaken for a complete one. Every one of these feeds only
# ATTEMPTED indicators — nothing REQUIRED depends on them, which is what makes
# continuing honest rather than lenient.
NETWORK_FAILED=0
CENSUS_FAILED=0
AMENITIES_FAILED=0
PLACES_FAILED=0

# ── 0. Preflight ─────────────────────────────────────────────────────────────
#
# Named individually. "the mirror is missing" sends someone to re-download 17 GB
# when one 52 KB file is absent.
step "0. Checking the lake mirror and the toolchain"
#
# Split into required and optional, because they are not the same kind of
# missing. Without marco there are no analysis units and *no* indicator can
# run, so the script should not start. Without the census or the amenities,
# only ATTEMPTED indicators lose inputs — and those are reported unavailable
# rather than failing. Treating all three as fatal would refuse to build a
# real pilot over a 52 KB file.
MISSING=0
check_required() {
  if [[ -e "$1" ]]; then
    note "ok        $1"
  else
    note "MISSING   $1"
    note "          $2"
    MISSING=1
  fi
}
check_optional() {
  if [[ -e "$1" ]]; then
    note "ok        $1"
  else
    note "absent    $1"
    note "          $2"
  fi
}
check_required data/sources/azure/inegi/marco_geostadistico/2024 \
  "INEGI blocks and AGEBs — no analysis units, so no indicator can run at all"
check_optional data/sources/azure/inegi/censo/manzanas_2020.parquet \
  "2020 census — the edificability family loses its population input"
check_optional data/sources/azure/exe/002_mexicali/equipamientos.geoparquet \
  "IMIP amenities — the accessibility family loses its destinations"

# ⚠ The vintage the pipeline reads and the vintage that was downloaded are not
# the same thing. `inegi_censo.DEFAULT_SOURCE_ROOT` points at
# manzanas_2020.parquet; docs/AZURE_DATA_INVENTORY.md records
# manzanas_2010.parquet being pulled from the container. Say which is actually
# here, because the alternative is discovering it at step 4 — and 2010 has a
# different headline column set (no p_18ymas), so it is not a --source-root
# swap.
if [[ -d data/sources/azure/inegi/censo ]]; then
  note "censo vintages on disk: $(ls data/sources/azure/inegi/censo 2>/dev/null | tr '\n' ' ')"
fi

if (( MISSING )); then
  cat >&2 <<'EOF'

The INEGI Marco Geoestadístico mirror is absent, and it is the one input with
no substitute: it holds the manzanas and AGEBs that every shipped indicator is
computed on. The entries marked "absent" above are optional and would only
cost you ATTEMPTED indicators.

`data/` is gitignored, so a fresh clone never has the mirror; it comes from the
Azure container recorded in docs/AZURE_DATA_INVENTORY.md, pulled with Storage
Explorer.

Without it, two things still work:

  scripts/acceptance_run.sh
      fully synthetic, no network, all 11 indicators

  overture divisions / aoi-from-division / extract / load
      a real municipal boundary and ~343k real buildings on the map, with
      real provenance — but no census blocks, so no indicator
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
# Non-fatal for the same reason as step 7: it feeds only ATTEMPTED indicators,
# and aborting here would discard step 2's ~3 GB INEGI load. The likely
# failure is a vintage mismatch — see step 0's note.
if ! $CLI inegi-censo bootstrap --cve-ent "$CVE_ENT"; then
  CENSUS_FAILED=1
  note "the census load failed — continuing; the edificability family will report unavailable"
fi

step "5. Loading Mexicali amenities (IMIP / DENUE)"
# Non-fatal: destinations for the accessibility family only.
if ! $CLI points bootstrap --layers mexicali_amenities; then
  AMENITIES_FAILED=1
  note "the amenity load failed — continuing; the accessibility family will report unavailable"
fi

# ── 5b. Park, recreation and cultural destinations, from Overture ────────────
#
# ⚠ Without this step the park indicator is not a measurement. The IMIP layer
# above carries exactly five categories — Consultorio medico, Farmacia,
# Hospital, Guarderia, Cine — of which three are health, one is school, and
# NONE is park-like. The multi-source Dijkstra then has no start vertices, so
# every one of the ~19,000 blocks reports the 1,500 m cutoff, and the map draws
# a uniform choropleth that reads as a real and uniformly bad park-access
# surface. The absence looks exactly like a finding.
#
# --country MX is not optional. The Mexicali bbox reaches across the border
# into Calexico, El Centro and Heber: 156 of its 1,303 places are in the US,
# 36 of them parks. Without the filter a block in northern Mexicali is reported
# a few hundred metres from a park in California.
step "5b. Loading park and cultural destinations (Overture places)"
if ! $CLI overture places \
  --bbox "$MEXICALI_BBOX" \
  --country MX \
  --aoi-id "$AOI_ID" \
  --output-path data/overture_places_mexicali.parquet; then
  PLACES_FAILED=1
  note "the places load failed — continuing; park and culture accessibility will report the cutoff everywhere"
fi

# ── 6. Buildings, from Overture ──────────────────────────────────────────────
#
# The lake has no Mexicali buildings. Overture does, and its divisions theme is
# also where a boundary comes from on a machine with no mirror — see
# docs/superpowers/specs/MEXICALI_FROM_OVERTURE.md. Here the boundary is
# INEGI's, so only the buildings are fetched.
step "6. Extracting and loading Overture buildings"
BUILDINGS=data/overture_buildings_mexicali.parquet

# ⚠ Validated, not merely present. Reuse keyed on `-f` alone was wrong in a
# way that fails downstream rather than here: an extract interrupted mid-write
# leaves a truncated parquet, the next --keep run reports "reusing" it, the
# load writes however many rows survived, and the first thing to complain is
# `urban_form_block_building_density` — four steps later, about blocks.
#
# Reading the footer is enough to catch it: a truncated parquet has no valid
# metadata, and a zero-row one has nothing to load.
buildings_usable() {
  [[ -f "$BUILDINGS" ]] || return 1
  $PY - "$BUILDINGS" "$MEXICALI_BBOX" <<'PYEOF' 2>/dev/null
import sys
import pyarrow.parquet as pq

# Footer and the bbox column's statistics only — no row data, so this costs
# milliseconds even on a 722 MB file.
#
# ⚠ Row count alone is not enough, and that is not hypothetical: a perfectly
# valid 722 MB extract of MEXICO CITY passed a rows>0 check, loaded, was
# clipped to nothing against the Mexicali AOI, and surfaced four steps later
# as "the global:building feature layer is empty". Checking the extent is what
# distinguishes "a readable parquet" from "this city's parquet".
path, bbox = sys.argv[1], sys.argv[2]
want = [float(v) for v in bbox.split(",")]
md = pq.ParquetFile(path).metadata
if md.num_rows <= 0:
    raise SystemExit(1)

names = pq.ParquetFile(path).schema_arrow.names
if "bbox" not in names:
    # No bbox struct to check against; accept on row count alone rather than
    # reject a file we cannot judge.
    raise SystemExit(0)

# Overture stores bbox as a struct; its leaf columns appear as bbox.xmin etc.
leaves = {md.row_group(0).column(i).path_in_schema: i
          for i in range(md.row_group(0).num_columns)}
try:
    xmin = min(md.row_group(g).column(leaves["bbox.xmin"]).statistics.min
               for g in range(md.num_row_groups))
    xmax = max(md.row_group(g).column(leaves["bbox.xmax"]).statistics.max
               for g in range(md.num_row_groups))
    ymin = min(md.row_group(g).column(leaves["bbox.ymin"]).statistics.min
               for g in range(md.num_row_groups))
    ymax = max(md.row_group(g).column(leaves["bbox.ymax"]).statistics.max
               for g in range(md.num_row_groups))
except (KeyError, AttributeError, TypeError):
    raise SystemExit(0)

# Reject only a clear disjoint — a narrower extract of the right city is fine.
overlaps = (xmax >= want[0] and xmin <= want[2]
            and ymax >= want[1] and ymin <= want[3])
if not overlaps:
    print(f"extent {xmin:.3f},{ymin:.3f},{xmax:.3f},{ymax:.3f} "
          f"does not overlap {bbox}", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
PYEOF
}

if buildings_usable; then
  note "reusing $BUILDINGS ($($PY -c '
import sys, pyarrow.parquet as pq
print(f"{pq.ParquetFile(sys.argv[1]).metadata.num_rows:,} rows")' "$BUILDINGS"))"
else
  if [[ -f "$BUILDINGS" ]]; then
    note "⚠ $BUILDINGS is unreadable, empty, or covers the wrong extent — re-extracting"
    rm -f "$BUILDINGS"
  fi
  # ⚠ --low-memory, and it is the right default *here* even though the CLI's
  # own default is off. Measured on this bbox: 663 MB / 454 s against
  # 4,457 MB / 40 s. The 4.4 GB is parquet metadata, not rows, and on a
  # laptop also running Postgres it does not fail cleanly — it swaps, which
  # presents as a hung process with no output, because the fast path writes
  # nothing until it is completely finished. Seven minutes of visible progress
  # beats forty seconds that might be forty minutes.
  note "~387k rows in MEXICALI_BBOX"
  # ⚠ --bbox is passed explicitly and MUST be. `overture extract`'s own
  # default is MEXICO_CITY_BBOX, 1,600 km away. Omitting it extracted several
  # million Mexico City buildings (a 722 MB file), `overture load --aoi-id`
  # then clipped every single one against the Mexicali polygon, and the run
  # reached step 10 with zero buildings and no error anywhere — the clip had
  # done precisely what it exists to do.
  #
  # Memory/speed is the operator's call, not this script's. Measured on this
  # bbox: default 4,457 MB / 40 s, --low-memory 663 MB / 454 s. This passed
  # --low-memory unconditionally, which imposed an 11x slowdown on every
  # machine to protect one whose RAM had been guessed at rather than measured.
  # Default is now fast; --low-memory is there for a machine where 4.4 GB
  # alongside Postgres swaps.
  if (( LOW_MEMORY )); then
    note "bounded-memory extract: ~663 MB, ~8 min"
    $CLI overture extract --output-path "$BUILDINGS" --bbox "$MEXICALI_BBOX" --low-memory
  else
    note "fast extract: ~4.4 GB peak, ~40 s. Pass --low-memory if that is too much"
    $CLI overture extract --output-path "$BUILDINGS" --bbox "$MEXICALI_BBOX"
  fi
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
  accessibility_block_culture_network_m
  livability_block_score
)

# ⚠ stderr is kept, not discarded, and that is a correction.
#
# This was `2>/dev/null`, so an indicator that *failed* reported
# "NONE REPORTED" with its reason thrown away — in the one step whose purpose
# is to say what went wrong. On the first real Mexicali run that is exactly
# what happened: a required indicator died and the message that would have
# explained it had already been discarded.
#
# stderr now goes to a file and is printed when, and only when, the parse
# finds no row count. Printing it unconditionally would bury a green run in
# progress chatter.
INDICATOR_ERR="$(mktemp "${TMPDIR:-/tmp}/reimagina-indicator-err.XXXXXX")"
rows_for() {
  : >"$INDICATOR_ERR"
  $CLI indicators compute --slug "$1" --aoi-id "$AOI_ID" 2>"$INDICATOR_ERR" \
    | tr -d '\n' | grep -oE '"rows_written": *[0-9]+' | grep -oE '[0-9]+$' || true
}

show_indicator_error() {
  if [[ -s "$INDICATOR_ERR" ]]; then
    echo "  ---- stderr from $1 ----" >&2
    # Tail rather than the whole traceback: the exception and its message are
    # the last lines, and the frames above are mostly typer and click.
    tail -25 "$INDICATOR_ERR" | sed 's/^/  /' >&2
    echo "  ---- end stderr ----" >&2
  fi
}

for slug in "${REQUIRED[@]}"; do
  rows="$(rows_for "$slug")"
  printf '  %-44s %s\n' "$slug" "rows_written=${rows:-NONE REPORTED}"
  # Parsed and compared, not grepped for non-emptiness: `"rows_written": 0` is
  # a non-empty match, so a grep-only check passes an indicator that computed
  # nothing — the one thing this step exists to catch.
  if [[ -z "$rows" ]]; then
    echo "  ^ required indicator reported no rows_written at all" >&2
    show_indicator_error "$slug"
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
    # One line of reason, not a traceback: an ATTEMPTED indicator failing is
    # expected, but "unavailable" with no cause is unreadable when it is
    # unexpected. The last stderr line is the exception message.
    why="$(tail -1 "$INDICATOR_ERR" 2>/dev/null | cut -c1-100)"
    printf '  %-44s %s\n' "$slug" "unavailable — ${why:-inputs absent on real data}"
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
if (( NETWORK_FAILED || CENSUS_FAILED || AMENITIES_FAILED || PLACES_FAILED )); then
  note "⚠ INCOMPLETE — some optional inputs did not load:"
  (( NETWORK_FAILED ))   && note "  - OSM network: road_edge is empty, so the map has no street context"
  (( CENSUS_FAILED ))    && note "  - 2020 census: no population observations"
  (( AMENITIES_FAILED )) && note "  - IMIP amenities: no school or health destinations"
  # Spelled out, because this one fails by looking like a result: the
  # indicators still write a row per block, all of them the 1,500 m cutoff.
  (( PLACES_FAILED ))    && note "  - Overture places: no park or cultural destinations, so those two indicators report 1500 m everywhere — that is the cutoff, not a distance"
  note "  Everything the REQUIRED indicators reported above is real."
fi
if (( ${#SKIPPED[@]} )); then
  note "unavailable on real data (${#SKIPPED[@]}): ${SKIPPED[*]}"
  note "each needs an attribute no INEGI or Overture layer carries; see step 10's comment"
fi
note "map: REIMAGINA_APP_DSN=$DATABASE_URL $PY -m apps.api  +  (cd apps/web && npm run dev)"
