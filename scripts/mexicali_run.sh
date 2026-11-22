#!/usr/bin/env bash
#
# Build the Mexicali pilot from real data, from the source bundle.
#
# The sibling of scripts/acceptance_run.sh: acceptance generates a synthetic
# territory; this one loads a real city, and every number it produces is
# about Mexicali. The only network access is the one-time bundle download
# from github.com; with --bundle, or once the bundle is cached, none.
#
# What it needs:
#   - PostgreSQL with PostGIS and pgRouting
#   - the Mexicali source bundle: INEGI Marco Geoestadístico 2024 and Censo
#     2020 for Baja California, the INEGI DENUE amenity layer, the Overture
#     buildings and places extracts and the OSM walking network, with
#     MANIFEST.json, ATTRIBUTION.md and SHA256SUMS. Without --bundle it is
#     fetched from its GitHub Release (checksum-pinned in
#     scripts/build_mexicali_bundle.py) into data/bundles/; --bundle DIR uses
#     a local copy and needs no network. It can also be rebuilt from INEGI's
#     public downloads with scripts/build_mexicali_bundle.py (see
#     docs/BOOTSTRAP.md).
#
# Usage:
#   scripts/mexicali_run.sh                       # fetch the bundle, rebuild $DATABASE_URL
#   scripts/mexicali_run.sh --bundle DIR          # offline, from a local bundle
#   scripts/mexicali_run.sh --keep                # load into the existing database
#   scripts/mexicali_run.sh --help                # this text
#
# The target is the database DATABASE_URL names (from .env; an exported value
# wins), which DATABASE_URL_DIRECT must name too. Without --keep it is DROPPED
# and recreated through DATABASE_URL_ADMIN, which defaults to the `postgres`
# database on the same server.
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
BUNDLE=""
while (( $# )); do
  case "$1" in
    --keep) KEEP=1 ;;
    --bundle) BUNDLE="${2:-}"; shift ;;
    --bundle=*) BUNDLE="${1#--bundle=}" ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Absolute before the cd below, so a path relative to the caller still works.
if [[ -n "$BUNDLE" ]]; then
  BUNDLE="$(cd "$BUNDLE" 2>/dev/null && pwd)" || {
    echo "--bundle: no such directory" >&2
    exit 2
  }
fi
cd "$ROOT"

PY="${PYTHON:-uv run python}"

# No --bundle: fetch the pinned release asset. fetch prints the bundle
# directory and explains on stderr what to do by hand when it cannot.
if [[ -z "$BUNDLE" ]]; then
  BUNDLE="$($PY scripts/build_mexicali_bundle.py fetch)" || exit 1
fi
CLI="$PY -m apps.cli.app"

# Parse .env as data; exported caller values always win.
ENV_EXPORTS="$($PY scripts/runtime_env.py harness)"
eval "$ENV_EXPORTS"
unset ENV_EXPORTS

step() { printf '\n\033[1m=== %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# INEGI codes are zero-padded strings: "02" is Baja California, not 2, and the
# parquet is Hive-partitioned on the padded value.
CVE_ENT=02
CVE_MUN=002
AOI_NAME="Mexicali"
PROJECT_NAME="Mexicali Pilot"

# ── 0. The bundle ────────────────────────────────────────────────────────────
#
# Checked before anything is dropped: a truncated or altered file fails here
# by name rather than four steps later as an indicator with odd numbers.
step "0. Verifying the source bundle against its SHA256SUMS"
$PY scripts/build_mexicali_bundle.py verify "$BUNDLE"

# One extract of each kind; the Overture release is in the file name and is
# the version the source rows record.
only_one() {
  local matches=("$@")
  if (( ${#matches[@]} != 1 )) || [[ ! -e "${matches[0]}" ]]; then
    echo "expected exactly one match, found: ${matches[*]}" >&2
    exit 1
  fi
  printf '%s' "${matches[0]}"
}
MARCO="$BUNDLE/inegi/marco_geoestadistico_2024"
CENSO="$BUNDLE/inegi/censo_2020/manzanas_2020.parquet"
BUILDINGS="$(only_one "$BUNDLE"/overture/buildings_*.parquet)"
PLACES="$(only_one "$BUNDLE"/overture/places_*.parquet)"
OSM_GRAPH="$(only_one "$BUNDLE"/osm/walk_network_*)"
RELEASE="${BUILDINGS##*/buildings_}"
RELEASE="${RELEASE%.parquet}"
note "Overture release $RELEASE; OSM network ${OSM_GRAPH##*/}"

# ── 1. Schema ────────────────────────────────────────────────────────────────
if [[ $KEEP -eq 0 ]]; then
  # Resolve the CLI and its dependencies before terminating any DB sessions.
  $CLI status >/dev/null
  step "1a. Dropping and recreating ${DB_NAME}"
  $PY scripts/runtime_env.py drop
fi

step "1b. Migrating and seeding the catalogs"
$CLI bootstrap-db
$CLI verify-schema
$CLI feat seed-types
$CLI indicators seed-catalog

# ── 2. INEGI geometry ────────────────────────────────────────────────────────
step "2. Loading INEGI Marco Geoestadístico for Baja California (CVE_ENT=${CVE_ENT})"
$CLI inegi-marco bootstrap \
  --source-root "$MARCO" \
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
$CLI inegi-censo bootstrap --source-root "$CENSO" --cve-ent "$CVE_ENT"

step "5. Loading Mexicali amenities (INEGI DENUE)"
$CLI points bootstrap --source-root "$BUNDLE" --layers mexicali_amenities

# ── 5b. Park, recreation and cultural destinations, from Overture ────────────
#
# ⚠ Without this step the park indicator is not a measurement. The DENUE layer
# above carries exactly five categories — Consultorio medico, Farmacia,
# Hospital, Guarderia, Cine — of which three are health, one is school, and
# NONE is park-like. The multi-source Dijkstra then has no start vertices, so
# every one of the ~19,000 blocks reports the 1,500 m cutoff, and the map draws
# a uniform choropleth that reads as a real and uniformly bad park-access
# surface. The absence looks exactly like a finding.
#
# The bundled extract was cut with --country MX: the Mexicali box reaches
# across the border into Calexico, and a US park is not one a Mexicali block
# walks to.
step "5b. Loading park and cultural destinations (Overture places)"
$CLI overture places \
  --raw-path "$PLACES" \
  --release "$RELEASE" \
  --aoi-id "$AOI_ID"

# ── 6. Buildings, from Overture ──────────────────────────────────────────────
#
# INEGI publishes no building footprints; Overture does. The bundled extract
# is the Mexicali box, clipped to the AOI on load.
step "6. Loading Overture buildings"
$CLI overture load \
  --raw-path "$BUILDINGS" \
  --release "$RELEASE" \
  --source-name "Overture Buildings - Mexicali BBox" \
  --aoi-id "$AOI_ID"

# ── 7. The street network ────────────────────────────────────────────────────
#
# The bundled snapshot of what `network bootstrap --clip-bbox` fetched from
# Overpass, so no network is needed. The fetch was clipped to the urban box:
# the municipality is 9,971 km2 reaching San Felipe, almost all desert.
# Membership, overlaps and every indicator still scope to the full boundary.
step "7. Loading the OSM pedestrian network and publishing it as roads"
$CLI network bootstrap --aoi-id "$AOI_ID" --graph-dir "$OSM_GRAPH"

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

# ── 9b. The baseline of the scenario inputs ──────────────────────────────────
#
# A scenario rule overrides storeys, m² per dwelling and people per household;
# the scenario workspace compares it against the baseline, so the baseline
# needs them too. Real where a source has them, synthetic and fitted to the
# census where not (scripts/seed_demo_attributes.py says how):
#
#   people per household  INEGI 2020 `prom_ocup`, ~71% of Mexicali's blocks
#   storeys               Overture `num_floors`, median per block — 83 blocks
#   everything else       synthetic: storeys from the tagged buildings' mix,
#                         m² per dwelling so each block's dwellings equal the
#                         census count (340,169 over Mexicali)
#
# Real values land as observations and shadow the synthetic jsonb, so the
# order of these three does not matter; the fit reads the real heights, so it
# runs last.
step "9b. Loading the baseline of the scenario inputs"
$CLI inegi-censo bootstrap --source-root "$CENSO" --cve-ent "$CVE_ENT" --columns prom_ocup
$CLI overture block-storeys --aoi-id "$AOI_ID"
$PY scripts/seed_demo_attributes.py --aoi-id "$AOI_ID" --fit-to-census

# ── 10. Indicators ───────────────────────────────────────────────────────────
#
# ⚠ Unlike acceptance_run.sh, this does NOT require all twelve to produce rows,
# and the difference is not laziness. The synthetic territory is built to
# satisfy every indicator's inputs; a real city is not. `planned_floors` and
# the edificability family read scenario inputs that step 9b supplies, mostly
# synthetically; if 9b failed they have nothing to read, and that is a fact
# about the inputs rather than a broken run.
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
if (( ${#SKIPPED[@]} )); then
  note "unavailable on real data (${#SKIPPED[@]}): ${SKIPPED[*]}"
  note "each reads a scenario input step 9b supplies; if 9b failed, see its comment"
fi
note "map: make dev (uses the configured environment)"
