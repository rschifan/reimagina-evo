# Spec: real Mexicali data, from the one upstream that is reachable

Load the second pilot city from real third-party data instead of a
synthetic fixture, using Overture Maps for both the boundary and the
buildings — because it is the only source in the repository's manifest
that this environment can actually reach.

**Status:** spec. Written before the code.

**Scope:** one stale constant corrected, one new module
(`reimagina/pipeline/overture_divisions.py`), two CLI commands, a Mexicali
bbox constant, and tests. **No migration**, no change to `serving_geo`, no
change to `get_tile`, no new feature type.

**Anti-scope:**

- **No INEGI.** `www.inegi.org.mx` is refused at CONNECT by this
  environment's network policy, and INEGI publishes shapefile ZIPs rather
  than the hive-partitioned parquet with WKB `BLOB` geometry that
  `pipeline/inegi_marco.py` reads. Widening the allowlist would not make
  that reader work.
- **No blocks or AGEBs, and therefore no indicator run.** Every shipped
  indicator is block-grain. INEGI manzanas are the analysis units, and
  they are not obtainable here. This delivers a real map of real
  buildings; it does not deliver a Mexicali indicator. See "What this does
  not give you".
- **No Overture `places` ingest.** Overture has 43,037 places in the
  Mexicali bbox and they would land as `global:establishment`, but the
  theme's column set is nothing like the `id, category, capacity,
  geometry, fuente, fecha, entidad` that `pipeline/points.py` expects.
  That is its own module, not a flag on this one.
- **No Overture `transportation` ingest.** It would replace the Overpass
  dependency in `osm_network.py` and feed `road_edge` — and therefore the
  street layer — from a reachable host. Genuinely worth doing and
  genuinely separate.
- **No change to the Azure lake path.** `pipeline/points.py` and
  `pipeline/inegi_marco.py` keep reading `data/sources/azure/…`. Nothing
  here replaces them; this is a second route to a city, not a migration
  away from the first.

**Honest scale:** about 90 lines of division selection, 40 of AOI
wiring, two CLI commands, and tests. The ingest path it feeds —
bbox extract, Bronze/Silver staging, curated load, AOI clip — already
exists and already takes `--bbox`, `--source-name` and `--aoi-id`. This
spec adds the one thing it was missing: a boundary polygon that does not
come from INEGI.

---

## Why, and why this way

The repository's data manifest names four local Azure-lake paths, an
Overture S3 release, and WorldPop's REST API. In this environment:

| Upstream | Status |
|---|---|
| `data/sources/azure/…` (4 paths) | **missing** — `data/` is gitignored, "never tracked", so no clone can carry it |
| `www.inegi.org.mx` | **403 at CONNECT** — environment network policy |
| `overpass-api.de` | **403 at CONNECT** — environment network policy |
| `www.worldpop.org` | **403 at CONNECT** — environment network policy |
| `overturemaps-us-west-2.s3.amazonaws.com` | **200**, and anonymous pyarrow S3 works through the proxy |

So there is exactly one reachable real upstream, and the question is
whether it is enough. Measured against release `2026-10-21.0` over the
Mexicali bbox (−115.70, 32.40, −115.20, 32.80):

| theme/type | rows in bbox |
|---|---|
| `buildings/building` | **387,273** |
| `places/place` | 43,037 |
| `divisions/division_area` | 16 |

Those 16 divisions are the finding. They include
**`county · MX · Municipio de Mexicali`** — a real municipal boundary
polygon, from the same reachable release, under the same licence as the
buildings.

**That removes the INEGI dependency for this city.** Every AOI
constructor in the repository today is INEGI-derived — `aoi_from_alcaldia`
looks up a `MEX:municipality` feature by `(CVE_ENT, CVE_MUN)`, and
`aoi_from_state` by `CVE_ENT`. Both return `missing_municipality` with no
lake mirror on disk. Overture divisions is a second constructor with no
such precondition.

### Why an AOI is not optional

`get_tile` scopes every request by AOI through an `EXISTS` against
`curated_geo.aoi_feature`. Features in no AOI are correctly typed,
correctly zoomed, present in `serving_geo` — and invisible, returning
`204 No Content` on every tile. That failure has already been paid for
once in this repository, by the street layer, and it produces a symptom
identical to a broken layer with no error anywhere in the path.

So "load Mexicali buildings" without a boundary is not a partial result.
It is 387,273 rows that cannot be drawn.

### Why spatial membership is already correct

`create_aoi` takes a `membership_key`, and r040 made membership
identifier-based where identifiers exist: a block belongs to an alcaldía
because INEGI says so, not because its polygon overlaps. An
Overture-derived AOI has no INEGI code to declare, so it passes
`membership_key=None` — and the membership SQL already handles that, in
all three geometry branches:

```sql
AND (
  aoi.membership_key IS NULL
  OR NOT (f.attributes ?& ARRAY(
       SELECT jsonb_object_keys(aoi.membership_key)))
  OR f.attributes @> aoi.membership_key
)
```

The comment above it names the case: *"one that carries none of them
(Overture buildings, OSM establishments) is decided spatially as
before."* This spec is the case that comment was written for. Nothing in
the membership path changes.

### The stale pin, which is a live defect either way

```python
OVERTURE_DEFAULT_RELEASE = os.environ.get("REIMAGINA_OVERTURE_RELEASE", "2026-07-22.0")
```

S3 holds `2026-09-23.0`, `2026-09-23.1`, `2026-10-21.0`. The pinned
release is gone, so **every Overture extract fails today**, for any city.

`overture.py`'s own docstring predicted this in detail — *"a pin here has
a shelf life measured in weeks and will stop resolving — this is the
second time it has"* — and built `available_overture_releases()` so the
failure names what S3 actually has. It was right; this is the third time.
The pin moves to `2026-10-21.0`, verified present, and the docstring's
count goes to three.

The pin stays a pin rather than becoming "latest", for the reason already
recorded there: the value is written to `data_source_catalog.version` and
to every row's `dataset_version`, and a moving default would make two
ingests of "the same" data incomparable.

---

## Design

### Division selection

```python
find_division(
    *, release, bbox, subtype="county", country="MX", name="Municipio de Mexicali"
) -> DivisionMatch | None
```

- Reads `theme=divisions/type=division_area`, filtered on the GeoParquet
  `bbox` struct columns, same cloud-native row-group pruning the buildings
  extract uses.
- Matches on `subtype`, `country`, and `names.primary`. All three,
  because the bbox alone returns 16 divisions spanning two countries —
  `US · Imperial County` and `MX · Municipio de Mexicali` are both
  `county`, and picking the first `county` row would be a coin flip that
  loads the wrong country's boundary.
- Returns the Overture id, the names struct, and the geometry as WKT —
  the form `create_aoi` already takes.
- `None` rather than an exception when nothing matches, so the CLI can
  report *what was in the bbox* instead of a traceback. A name that does
  not match is the expected failure here: Overture's `names.primary`
  is upstream data and can be retitled between releases.

### AOI construction

```python
aoi_from_overture_division(dsn, *, release, bbox, subtype, country, name,
                           aoi_name, project_name) -> dict
```

Mirrors `apps/cli/cdmx.py`'s `aoi_from_alcaldia` — same `_ensure_dev_project`
shape, same `create_aoi` call, same `{"status": ...}` payload a caller can
branch on — with `membership_key=None` and a description that records the
release and the Overture division id, so the AOI says where its boundary
came from.

### Mexicali as a named constant

```python
MEXICALI_BBOX = (-115.70, 32.40, -115.20, 32.80)
MEXICALI_DIVISION = "Municipio de Mexicali"
```

Beside `MEXICO_CITY_BBOX`, which is already a module constant. A bbox
typed by hand on the command line is a number nobody can check and
nobody can reproduce; a constant is reviewable and appears in the
provenance of what it loaded.

⚠ The bbox spans the international border — `US · Calexico` and
`US · El Centro` are inside it, and a share of those 387,273 buildings is
American. That is not a defect to fix in the bbox: the extract is
deliberately a superset, and `_clip_buildings_to_aoi` deletes rows whose
geometry falls outside the AOI polygon after load, precisely so
"bbox-but-not-polygon orphans never persist". The Mexican municipal
boundary is what decides, and the count before and after the clip is
worth recording as evidence that it did.

### CLI

- `overture divisions --bbox … --release …` — list what Overture has in a
  bbox, with subtype, country and name. The command that makes the name
  argument checkable instead of guessable.
- `overture aoi-from-division --name "Municipio de Mexicali" …` — create
  the AOI.

---

## What this does not give you

> **⚠ Superseded by the delivery machine, and the spec was wrong in the
> interesting direction.** This section said no indicator could run. That
> held for *this environment*, where INEGI is refused at CONNECT — and the
> inference I drew from it, that the data was unavailable, did not. The lake
> mirror was on the author's workstation the whole time: 17 GB including the
> ~3 GB Marco Geoestadístico. Run there, INEGI contributed 68,984 blocks and
> 1,824 AGEBs, and `urban_form_block_compactness`,
> `urban_form_block_building_density` and `urban_form_block_composite` each
> computed 19,058 rows on real census blocks, with 313,827 of these Overture
> buildings attributed to them. See Appendix A of
> `docs/delivery/05_capability_proof.md`.
>
> The lesson worth keeping: "this environment cannot reach it" is a fact
> about the environment. I wrote it down as a fact about the data, and the
> spec's scope was narrower than it needed to be for a day because of it.
>
> What survives unchanged is the part that was actually load-bearing — an
> Overture-derived AOI needs no INEGI mirror, which is what makes this route
> work on a machine that has none.

**No indicator can run on Mexicali from this data alone.** Every shipped
indicator is block-grain, and the analysis units are INEGI manzanas, which
are not reachable *from this environment*. `urban_form_block_compactness` and
`urban_form_block_building_density` both need a block layer that this spec
does not produce.

What it does give is the pipeline exercised end to end against real,
licensed, city-scale third-party data — 387,273 candidate features rather
than 40 synthetic edges — with a real administrative boundary, real
provenance in `data_source_catalog`, and derived attribution. For a
delivery whose capability proof currently runs entirely on a generated
territory, that is the gap worth closing. It is not the same as claiming
Mexicali is analysed.

Stating this in the spec rather than discovering it after the load is the
point of writing the spec first.

---

## The risk I am not closing

`_extract_remote_overture_subset` materialises the whole bbox subset in
memory — `pq.write_table(dataset.to_table(filter=flt), …)` — and the
module already flags it: *"fine for a city bbox; switch to
dataset.to_batches() + ParquetWriter if a larger AOI OOMs."*

387k Overture building rows carry geometry, a `names` struct and a
`sources` list. I will measure the peak and the output size on this run
and record both. I am not going to pre-emptively rewrite the extract to
be streaming, because the existing note already says what to do and a
measurement is worth more than a guess about which AOI is "larger". If it
OOMs here, that is the measurement, and the streaming rewrite becomes
this spec's second commit rather than its speculation.

---

## What the implementation found

### 1. The silver filename was named for the wrong city

The first real Mexicali load landed
`overture_buildings_mexicali.parquet` in Bronze — which takes its name
from the raw file — and then wrote
`overture_buildings_mexico_city_silver.parquet` beside it, because the
Silver default was the Mexico City module constant. Two stages of one
ingest disagreeing about which city they held, in the layer whose whole
job is traceability, and both paths are returned in the load result.

`silver_filename_for()` now derives the name from the Bronze artifact, so
the two cannot drift for any AOI. The CDMX name is unchanged, which has a
test. The dead `bronze_filename` parameter — accepted and never passed to
`bronze.land_file` — went with it.

### 2. The border clip is load-bearing, and measurable

43,577 of 387,273 buildings were deleted by `_clip_buildings_to_aoi`:
the US side of a bbox that deliberately crosses the border. The loaded
extent proves it — `ymax` 32.676 from a bbox `ymax` of 32.80, i.e. the
international border, not the box. Without the clip, a Mexicali AOI
would have contained Calexico.

### 3. Membership at city scale costs nine minutes

`aoi compute-features` inserted 343,696 `aoi_feature` rows in **8m56s**,
against a 2,109-point MultiPolygon. Every loaded building is a member,
so the layer draws — but this is the first measurement of that pass on
real city-scale data, and it is three orders of magnitude slower than on
the synthetic fixture. Recorded rather than optimised: it is a one-time
cost per load, it is not on any request path, and a guess at which part
of `ST_Intersection`-per-feature dominates would be a guess.

### 4. r038's building floor is what makes this viewable at all

Decoded tiles over Centro Cívico, through `serving_geo.get_tile` with the
AOI filter the frontend always sends:

| z | bytes | features |
|---|---|---|
| 8 | 0 | — |
| 11 | 0 | — |
| 12 | **0** | — |
| 14 | 393,027 | 5,562 |
| 16 | 27,785 | 378 |

z12 returning nothing is `r038`'s `type_slug` zoom floor for buildings,
which was chosen from a CDMX measurement of 2.59 MB per z12 tile. On real
Mexicali data a z14 tile is already 393 KB; the floor is not a
nicety. This is also the first evidence from a second city that the
number `r038` picked generalises.

---

## Tests

| Layer | What |
|---|---|
| `tests/unit` | the pinned release is one S3 actually serves; division matching requires subtype **and** country **and** name — the test that fails if `US · Imperial County` could ever be selected for `MX`; `MEXICALI_BBOX` contains the known municipal seat and the division name is non-empty |
| `tests/integration` | an AOI built from a division WKT gets `membership_key = NULL`; a polygon feature inside it becomes a member spatially and one outside does not; the AOI's description records release and division id |

The network-dependent probes stay out of the suite. A test that fails
when Overture rotates a release is a test that fails for a reason
unrelated to the code under it; the pin's validity is checked by
`data verify-sources`, which is where that check belongs.

## Maintenance checklist

1. No migration — no new type, no new view, no new tile function.
2. `serving_geo` unchanged.
3. `explore` unchanged; `feature_type_stats_v` picks up the new source
   row and dataset version on its own.
4. CLI gains two commands under the existing `overture` group.
5. Tests added.
6. `AGENTS.md`, `docs/BOOTSTRAP.md` and `docs/AZURE_DATA_INVENTORY.md`
   updated — the last one because it currently presents the lake as the
   only route to Mexicali, and after this it is not.
