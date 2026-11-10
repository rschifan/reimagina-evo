"""Overture Maps first-source helpers for Mexico City onboarding."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as pads
import pyarrow.parquet as pq
from pyarrow import fs as pafs

from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.storage import backend_for_connection
from . import bronze, silver
from .loader import load_vector_source


logger = logging.getLogger(__name__)

#: How often the per-file extract reports progress, in files.
#:
#: ⚠ There to stop a working command from looking like a hung one. The
#: low-memory path takes ~8 minutes over 512 files and printed nothing at all,
#: which is indistinguishable from the swapping it exists to avoid. 32 gives
#: ~16 lines over a release — enough to see movement, not enough to bury the
#: result.
_PROGRESS_EVERY_FILES = 32

MEXICO_CITY_BBOX = (-99.597, 18.992, -98.606, 19.921)

#: Mexicali urban core and its US-side surroundings, EPSG:4326.
#:
#: ⚠ **This bbox crosses the international border on purpose.** Calexico and
#: El Centro (California) fall inside it, so a share of the buildings it
#: returns are American. That is not a bug to fix by shrinking the box: the
#: extract is deliberately a superset of the AOI, and
#: :func:`_clip_buildings_to_aoi` deletes post-load whatever falls outside the
#: Mexican municipal polygon, so "bbox-but-not-polygon orphans never persist".
#: Shrinking it to hug the border would instead risk clipping Mexicali itself.
#:
#: A constant rather than a command-line argument because a bbox typed by hand
#: is a number no reviewer can check and no run can reproduce.
MEXICALI_BBOX = (-115.70, 32.40, -115.20, 32.80)

#: ``names.primary`` of the Mexicali municipal boundary in Overture's
#: ``divisions`` theme, as of release 2026-10-21.0. Upstream data, so it can
#: be retitled between releases — which is why `overture divisions` exists to
#: print what a release actually calls things.
MEXICALI_DIVISION_NAME = "Municipio de Mexicali"

#: Overture's subtype for a Mexican *municipio* / US county.
DIVISION_SUBTYPE_COUNTY = "county"
OVERTURE_PROVIDER = "Overture Maps Foundation"
OVERTURE_LICENSE = "ODbL-1.0 / Overture terms"
OVERTURE_BUILDINGS_SOURCE_NAME = "Overture Buildings - Mexico City BBox"
OVERTURE_BUILDINGS_TYPE = "building"
OVERTURE_BUILDINGS_THEME = "buildings"
OVERTURE_RELEASE_S3_ROOT = "s3://overturemaps-us-west-2/release"
OVERTURE_S3_REGION = "us-west-2"
# Default Overture release for ingest.
#
# ⚠ **Overture keeps only the most recent release on S3.** Older ones are
# deleted, so any pin here has a shelf life measured in weeks and *will* stop
# resolving — this is the **third** time it has: 2026-02-18.0 was never
# published, 2026-04-15.0 was published and then rotated away, and 2026-07-22.0
# went the same way, after which every extract failed with a bare
# `FileNotFoundError` naming a path.
#
# Kept as a pin anyway, because the value is recorded as `dataset_version` on
# the source and on every row's provenance, and a moving default would make two
# ingests of "the same" data incomparable. The cost is paid at fetch time
# instead: `available_overture_releases()` turns the failure into a message that
# names what S3 actually has, so bumping this is a ten-second fix rather than an
# investigation. Override per-run with REIMAGINA_OVERTURE_RELEASE.
#
# Verified against S3 on 2026-11-10, when the release listing held exactly
# 2026-09-23.0, 2026-09-23.1 and 2026-10-21.0.
OVERTURE_DEFAULT_RELEASE = os.environ.get("REIMAGINA_OVERTURE_RELEASE", "2026-10-21.0")
#: Rows per batch when streaming an extract out of S3.
#:
#: Trades peak memory against per-batch overhead. 50k Overture building rows
#: is a few hundred MB resident rather than the 4.4 GB the whole Mexicali
#: bbox took when materialised at once, and large enough that row-group
#: boundaries still dominate the read.
_EXTRACT_BATCH_ROWS = 50_000

OVERTURE_BUILDINGS_FILENAME = "overture_buildings_mexico_city.parquet"
#: Legacy Mexico City Silver name. No longer a default — `silver_filename_for`
#: derives the name from the Bronze artifact so the two cannot disagree about
#: which city they hold. Kept because it is referenced by the CDMX fixtures.
OVERTURE_BUILDINGS_SILVER_FILENAME = "overture_buildings_mexico_city_silver.parquet"
OVERTURE_BUILDINGS_NOTES = (
    "First example source: Overture Mexico City buildings subset clipped to the "
    "broader Mexico City bounding box."
)
OVERTURE_BUILDINGS_ATTRIBUTES = [
    "subtype",
    "class",
    "height",
    "num_floors",
    "names",
    "sources",
]
OVERTURE_PENDING_STORAGE_URI = "pending://overture/buildings/mexico-city"


@dataclass(frozen=True)
class OvertureSourceSpec:
    name: str
    provider: str
    storage_type: str
    integration_role: str
    version: str
    filename: str
    bbox: tuple[float, float, float, float]
    feature_type_context: str
    feature_type_slug: str
    notes: str
    license: str | None = None


OVERTURE_BUILDINGS_MEXICO_CITY = OvertureSourceSpec(
    name=OVERTURE_BUILDINGS_SOURCE_NAME,
    provider=OVERTURE_PROVIDER,
    storage_type="vector",
    integration_role="canonical_entity",
    version=OVERTURE_DEFAULT_RELEASE,
    filename=OVERTURE_BUILDINGS_FILENAME,
    bbox=MEXICO_CITY_BBOX,
    feature_type_context="global",
    feature_type_slug="building",
    notes=OVERTURE_BUILDINGS_NOTES,
    license=OVERTURE_LICENSE,
)


def overture_theme_type_glob(
    *,
    release: str = OVERTURE_DEFAULT_RELEASE,
    theme: str = OVERTURE_BUILDINGS_THEME,
    feature_type: str = OVERTURE_BUILDINGS_TYPE,
) -> str:
    """Return the cloud GeoParquet glob for a pinned Overture release."""
    return (
        f"{OVERTURE_RELEASE_S3_ROOT}/{release}/"
        f"theme={theme}/type={feature_type}/*.parquet"
    )


def _s3_prefix_from_glob(source_glob: str) -> str:
    """Strip the ``s3://`` scheme and trailing ``/*.parquet`` glob so a
    pyarrow dataset / file selector can walk the release directory."""
    prefix = source_glob.split("://", 1)[-1]
    if prefix.endswith("/*.parquet"):
        prefix = prefix[: -len("/*.parquet")]
    return prefix


def available_overture_releases() -> list[str]:
    """Release folders currently on Overture's S3, oldest first.

    Overture deletes superseded releases, so this is the authoritative answer to
    "what can I actually pin to today" — and usually returns a single entry.
    """
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    root = OVERTURE_RELEASE_S3_ROOT.removeprefix("s3://")
    selector = pafs.FileSelector(root, recursive=False)
    return sorted(info.base_name for info in s3.get_file_info(selector))


def _extract_remote_overture_subset(
    source_glob: str,
    output_path: Path,
    bbox: tuple[float, float, float, float],
    low_memory: bool = False,
) -> None:
    """Read the pinned Overture S3 GeoParquet release and write the bbox
    subset locally. Filters on the GeoParquet ``bbox`` struct columns so
    pyarrow prunes non-overlapping row groups from row-group statistics
    without fetching them."""
    min_lon, min_lat, max_lon, max_lat = bbox
    s3 = pafs.S3FileSystem(anonymous=True, region=OVERTURE_S3_REGION)
    try:
        dataset = pads.dataset(
            _s3_prefix_from_glob(source_glob), filesystem=s3, format="parquet"
        )
    except FileNotFoundError as exc:
        # The pinned release has almost certainly been rotated off S3. Say so,
        # and say what is there — the bare error names a path and leaves the
        # reader to guess whether it is a typo, a permissions problem, or this.
        raise FileNotFoundError(
            f"Overture release not found: {exc}. Overture keeps only its most "
            f"recent release, so a pin older than a few weeks stops resolving. "
            f"Available now: {', '.join(available_overture_releases()) or '(none)'}. "
            f"Set REIMAGINA_OVERTURE_RELEASE, or bump OVERTURE_DEFAULT_RELEASE "
            f"in pipeline/overture.py."
        ) from exc
    flt = (
        (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "ymax") >= min_lat)
        & (pc.field("bbox", "ymin") <= max_lat)
    )

    if low_memory:
        _write_subset_per_file(dataset, flt, output_path, s3, source_glob)
    else:
        # Materialises the bbox subset in one table. Fast, and the memory cost
        # is NOT the rows — see `low_memory` in
        # :func:`extract_overture_buildings_subset` for the measurement that
        # says where it actually goes.
        pq.write_table(dataset.to_table(filter=flt), str(output_path))


def _progress(message: str) -> None:
    """Report long-running progress.

    Just the logger. `reimagina.logging_setup.configure_logging` — which
    `apps/cli/app.py` calls — already sends `reimagina.*` records to stderr
    precisely so "stdout stays the machine-readable JSON result channel".

    The first version of this also wrote to stderr directly, on the reasoning
    that progress should not depend on the caller configuring logging. That
    printed every line twice under the CLI, which is the only caller that
    matters here. Using the convention the repository already has is both
    quieter and less code.
    """
    logger.info(message)


def _write_subset_per_file(
    dataset, flt, output_path: Path, filesystem, source_glob: str
):
    """Scan one file at a time so parquet metadata stays bounded.

    ⚠ **The expensive thing is metadata, not data.** Measured on the Mexicali
    bbox against release 2026-10-21.0 (512 files, 387,273 matching rows)::

        baseline interpreter               109 MB
        after pads.dataset()               132 MB   (1 s)
        after count_rows(filter)         4,170 MB   (139 s)  <-- here
        after draining 387,273 rows      4,578 MB   (4 s)

    Nearly all of it is spent evaluating the bbox predicate against row-group
    statistics across every file, before a single row is returned. Draining the
    rows afterwards costs ~400 MB.

    That rules out the two obvious fixes, both of which were tried and
    measured: writing in batches instead of one table (4,457 -> 4,304 MB) and
    capping `fragment_readahead`/`batch_readahead` (4,285 MB at 1/2). Neither
    touches metadata.

    Opening each file as its own single-file dataset and dropping it does. Peak
    held **flat at 663 MB** across all 512 files — bounded rather than
    proportional, which is the property that matters for an AOI larger than
    one city.

    The cost is wall time: 454 s against 40 s, because each file's footer is a
    separate round trip and most of them match nothing. On this release all
    387,273 rows came from the first 128 files; the remaining 384 were read for
    their statistics and discarded.
    """
    written = 0
    writer: pq.ParquetWriter | None = None
    files = list(dataset.files)
    total = len(files)
    _progress(f"extract: scanning {total} files one at a time (bounded memory)")
    try:
        for index, path in enumerate(files, start=1):
            one = pads.dataset(path, filesystem=filesystem, format="parquet")
            for batch in one.to_batches(filter=flt, batch_size=_EXTRACT_BATCH_ROWS):
                if batch.num_rows == 0:
                    # Survived statistics pruning, but no row passes the
                    # predicate. Skipping keeps a schema-only batch from
                    # opening the writer with nothing in it.
                    continue
                if writer is None:
                    # Schema taken from the first real batch, not the dataset:
                    # they differ when the release carries columns no row in
                    # this bbox populates.
                    writer = pq.ParquetWriter(str(output_path), batch.schema)
                writer.write_batch(batch)
                written += batch.num_rows
            # Explicit, because the metadata this holds is the whole point.
            del one
            if index % _PROGRESS_EVERY_FILES == 0 or index == total:
                _progress(f"extract:   {index}/{total} files, {written:,} rows")
    finally:
        if writer is not None:
            writer.close()

    if writer is None:
        # No rows anywhere. Write an empty file with the dataset's schema
        # rather than none: the caller stages this path into Bronze, and a
        # missing file fails with "no such file" instead of "the bbox is empty".
        pq.write_table(dataset.schema.empty_table(), str(output_path))


def _extract_local_overture_subset(
    source_glob: str,
    output_path: Path,
    bbox: tuple[float, float, float, float],
) -> None:
    """Filter a local GeoParquet to the bbox via geopandas ``.cx`` — a
    bounding-box spatial slice (geometry intersection with the bbox
    envelope)."""
    import geopandas as gpd

    min_lon, min_lat, max_lon, max_lat = bbox
    gdf = gpd.read_parquet(source_glob)
    gdf.cx[min_lon:max_lon, min_lat:max_lat].to_parquet(output_path)


def extract_overture_buildings_subset(
    *,
    output_path: str | Path = OVERTURE_BUILDINGS_FILENAME,
    source_glob: str | None = None,
    bbox: tuple[float, float, float, float] = MEXICO_CITY_BBOX,
    release: str = OVERTURE_DEFAULT_RELEASE,
    low_memory: bool = False,
) -> Path:
    """Extract an Overture buildings subset to local GeoParquet.

    Remote Overture releases (``s3://…``) are read with pyarrow over an
    anonymous S3 filesystem and filtered on the GeoParquet ``bbox`` struct
    columns for cloud-native predicate pushdown. A local GeoParquet
    ``source_glob`` falls back to a geometry bbox intersection via
    geopandas. The local output becomes the canonical Bronze artifact.

    Defaults match the legacy CDMX bbox. Override ``bbox`` for any other
    AOI (e.g. Mexicali). ``release`` selects the Overture S3 release
    folder; the default tracks the most recently verified snapshot
    (see :data:`OVERTURE_DEFAULT_RELEASE`).

    ``low_memory`` trades wall time for a bounded memory ceiling. Measured on
    the Mexicali bbox, release 2026-10-21.0, 512 files, 387,273 matching rows:

    ===========  ==========  =========
    path         peak RSS    elapsed
    ===========  ==========  =========
    default      4,457 MB    40 s
    low_memory     663 MB    454 s
    ===========  ==========  =========

    ⚠ The default's 4.4 GB is not the rows — those cost ~400 MB. It is parquet
    metadata, read while evaluating the bbox predicate against row-group
    statistics across all 512 files. See :func:`_write_subset_per_file` for
    the attribution and for the two cheaper fixes that were tried and did not
    work.

    Default stays off: 4.4 GB is affordable on a server and 454 s is not free.
    Turn it on where memory is the binding constraint — a laptop also running
    Postgres, where the failure is not a clean OOM but swapping, which
    presents as a hung process. ``scripts/mexicali_run.sh`` passes it for
    exactly that reason.

    The ceiling is flat rather than proportional (663 MB held across all 512
    files), so ``low_memory`` is also the only path that is known to survive an
    AOI materially larger than one city.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    effective_glob = source_glob or overture_theme_type_glob(release=release)
    if str(effective_glob).startswith("s3://"):
        _extract_remote_overture_subset(
            effective_glob, output_path, bbox, low_memory=low_memory
        )
    else:
        _extract_local_overture_subset(effective_glob, output_path, bbox)
    return output_path


def register_overture_buildings_source(
    conn,
    *,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
    version: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
) -> str:
    """Register an Overture Buildings source row. ``source_name`` lets a
    caller register one row per AOI (e.g. one for CDMX, one for Mexicali)
    rather than collapsing every extraction into the same catalog row."""
    return cat.register_source(
        conn,
        name=source_name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        storage_type=OVERTURE_BUILDINGS_MEXICO_CITY.storage_type,
        integration_role=OVERTURE_BUILDINGS_MEXICO_CITY.integration_role,
        storage_uri=storage_uri,
        version=version,
        license=OVERTURE_BUILDINGS_MEXICO_CITY.license,
        notes=notes,
    )


def ensure_overture_buildings_source(
    conn,
    *,
    version: str = OVERTURE_DEFAULT_RELEASE,
    storage_uri: str = OVERTURE_PENDING_STORAGE_URI,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
) -> str:
    """Idempotently ensure an Overture buildings source row exists. Keyed
    on (``source_name``, provider, ``version``) — distinct names produce
    distinct catalog rows."""
    existing = cat.get_source(
        conn,
        name=source_name,
        provider=OVERTURE_BUILDINGS_MEXICO_CITY.provider,
        version=version,
    )
    if existing is not None:
        if storage_uri and existing["storage_uri"] != storage_uri:
            cat.update_source_storage_uri(conn, str(existing["id"]), storage_uri)
        return str(existing["id"])
    return register_overture_buildings_source(
        conn,
        storage_uri=storage_uri,
        version=version,
        source_name=source_name,
        notes=notes,
    )


def silver_filename_for(bronze_path: str | Path) -> str:
    """Derive the Silver filename from the Bronze artifact's own name.

    ⚠ The default used to be the module constant, which is named for Mexico
    City. Bronze takes its name from the raw file, so a Mexicali load landed
    ``overture_buildings_mexicali.parquet`` in Bronze and then wrote
    ``overture_buildings_mexico_city_silver.parquet`` beside it — two stages
    of one ingest disagreeing about which city they held, in the layer whose
    whole job is traceability. The paths are recorded in the load result and
    in ``data_source_catalog.storage_uri``, so the wrong name propagates into
    provenance.

    Deriving it means the two names can no longer drift, for any AOI.
    """
    return f"{Path(bronze_path).stem}_silver.parquet"


def stage_overture_buildings_subset(
    raw_path: str | Path,
    *,
    source_id: str,
    silver_filename: str | None = None,
) -> tuple[Path, Path]:
    """Land a raw GeoParquet subset to Bronze and write a cleaned Silver GeoParquet.

    ``silver_filename`` defaults to :func:`silver_filename_for` of the Bronze
    artifact rather than to a constant; pass one only to override.
    """
    bronze_path = bronze.land_file(raw_path, source_id)
    gdf = silver.read_vector(bronze_path)
    silver_path = silver.save_silver_vector(
        gdf, source_id, silver_filename or silver_filename_for(bronze_path)
    )
    return bronze_path, silver_path


def bootstrap_overture_buildings_subset(
    conn,
    *,
    raw_path: str | Path,
    orchestrator_run_id: str,
    code_version: str = "dev",
    dataset_version: str = OVERTURE_DEFAULT_RELEASE,
    source_name: str = OVERTURE_BUILDINGS_SOURCE_NAME,
    notes: str = OVERTURE_BUILDINGS_NOTES,
    aoi_id: str | None = None,
) -> dict[str, Any]:
    """Run the full ingest flow: register, Bronze/Silver stage, curated
    load, and (optionally) clip the just-loaded buildings to an AOI
    polygon so bbox-but-not-polygon orphans never persist."""
    feat.seed_feature_types(conn)
    conn.commit()

    source_id = ensure_overture_buildings_source(
        conn, version=dataset_version, source_name=source_name, notes=notes
    )
    conn.commit()

    bronze_path, silver_path = stage_overture_buildings_subset(
        raw_path, source_id=source_id
    )
    cat.update_source_storage_uri(
        conn, source_id, bronze.bronze_uri(source_id, bronze_path.name)
    )
    conn.commit()

    load_result = load_overture_buildings_subset(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        dataset_version=dataset_version,
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
    )

    clip_result: dict[str, int | str | float | None] = {}
    if aoi_id is not None:
        clip_result = _clip_buildings_to_aoi(
            conn,
            ingestion_run_id=str(load_result["ingestion_run_id"]),
            aoi_id=aoi_id,
            loaded=int(load_result.get("records_written") or 0),
            source_id=source_id,
        )
        conn.commit()

    return {
        "source_id": source_id,
        "bronze_path": str(bronze_path),
        "silver_path": str(silver_path),
        **load_result,
        **clip_result,
    }


def load_overture_buildings_subset(
    conn,
    *,
    source_id: str,
    silver_path: str | Path,
    dataset_version: str = OVERTURE_DEFAULT_RELEASE,
    orchestrator_run_id: str,
    code_version: str = "dev",
) -> dict[str, str | int]:
    """Load a cleaned Overture buildings subset into curated_geo.feature."""
    type_id = feat.get_type_id(
        conn,
        OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_context,
        OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug,
    )
    return load_vector_source(
        conn,
        source_id=source_id,
        silver_path=silver_path,
        type_id=type_id,
        dataset_version=dataset_version,
        source_record_ref_col="id",
        orchestrator_run_id=orchestrator_run_id,
        code_version=code_version,
        name_col=None,
        attributes_cols=OVERTURE_BUILDINGS_ATTRIBUTES,
    )


class ExtractAoiMismatch(RuntimeError):
    """The extract's bbox and the AOI do not overlap, so the clip removed all of it."""


def _clip_buildings_to_aoi(
    conn,
    *,
    ingestion_run_id: str,
    aoi_id: str,
    loaded: int | None = None,
    source_id: str | None = None,
) -> dict[str, int | str | float | None]:
    """Drop rows from this ingestion run whose centroid falls outside the
    AOI polygon. Bbox-based S3 extraction always returns a superset of
    the AOI; this guarantees ``aoi_feature`` recompute won't surface
    bbox-only orphans later.

    ⚠ **Leaving the AOI empty is a mismatch, and it raises.**

    The clip exists because the bbox is a *superset* of the AOI. If, once it
    has run, this source has no features left inside the AOI at all, the bbox
    and the AOI are disjoint and the extract is of somewhere else — and the
    original behaviour was to report the deletion as a successful clip and
    carry on with an empty layer.

    ⚠ The first version of this guard tested the wrong thing: "the clip
    removed 100 % of what this run loaded". That is a false positive on a
    re-run, and it fired on one. A second `overture load` of the same file
    upserts — the rows that survived the first clip are skipped
    ``ON CONFLICT``, and the rows the first clip *deleted* are re-inserted. So
    run two loads exactly the rows that will be clipped again, and clipping
    100 % of them is correct idempotent behaviour. Measured: 387,273 loaded,
    43,566 re-inserted on the second pass, 343,707 already present and
    untouched.

    The invariant that actually distinguishes the two is about the AOI, not
    about the run: *after clipping, does this source have anything inside the
    AOI?* A re-run leaves 343,707 there; a Mexico City extract leaves nothing.

    That is not theoretical. `scripts/mexicali_run.sh` omitted `--bbox`, so
    `overture extract` used its own default — MEXICO_CITY_BBOX, 1,600 km
    away. Several million Mexico City buildings were extracted, loaded, and
    clipped to nothing against the Mexicali AOI. No error. The run continued
    for four more steps and surfaced as
    "urban_form_block_building_density needs the global:building feature
    layer, which is empty" — a message about blocks, pointing at the wrong
    thing entirely.

    Refusing here puts the failure where the cause is. It costs nothing
    legitimate: a load whose every row falls outside its own AOI has no
    correct interpretation.
    """
    cur = conn.cursor()
    cur.execute(
        """
        WITH aoi_fixed AS (
          -- ST_MakeValid, for consistency with `compute_overlaps`, which has
          -- repaired the AOI geometry this way since r004. This clip did not,
          -- and it is a DELETE — the one place an inconsistency costs data
          -- rather than a wrong answer. INEGI polygons are routinely
          -- self-intersecting, so the repair is not hypothetical.
          --
          -- ⚠ Hardening, NOT a diagnosis. The obvious theory — that an
          -- invalid AOI makes ST_Intersects reject everything — was tested
          -- and does not hold: a self-intersecting bowtie has ST_Area 0 yet
          -- ST_Intersects still returns true for a point on its surface.
          -- GEOS is more robust here than it looks. So this closes a real
          -- asymmetry between two code paths over the same geometry; it is
          -- not the explanation for any particular empty clip.
          --
          -- CollectionExtract(..., 3) keeps the polygonal parts: MakeValid on
          -- a self-intersecting polygon can return a collection with stray
          -- lines or points, and those would make the clip reject more than
          -- it should.
          SELECT CASE
                   WHEN ST_IsValid(geometry) THEN geometry
                   ELSE ST_CollectionExtract(ST_MakeValid(geometry), 3)
                 END AS geometry
          FROM curated_geo.area_of_interest
          WHERE id = %s
        )
        DELETE FROM curated_geo.feature f
        USING aoi_fixed aoi
        WHERE f.ingestion_run_id = %s
          AND NOT ST_Intersects(f.geometry, aoi.geometry)
        """,
        (aoi_id, ingestion_run_id),
    )
    clipped = cur.rowcount or 0

    remaining: int | None = None
    if loaded and source_id is not None:
        # Scoped to source_id rather than ingestion_run_id, because a re-run's
        # surviving rows belong to the *earlier* run. Counting this run alone
        # is what made the first version of this guard a false positive.
        cur.execute(
            """
            SELECT count(*)
            FROM curated_geo.feature f
            JOIN curated_geo.area_of_interest aoi ON aoi.id = %s
            WHERE f.source_id = %s
              AND f.type_slug = 'building'
              AND ST_Intersects(
                    f.geometry,
                    CASE
                      WHEN ST_IsValid(aoi.geometry) THEN aoi.geometry
                      ELSE ST_CollectionExtract(ST_MakeValid(aoi.geometry), 3)
                    END)
            """,
            (aoi_id, source_id),
        )
        remaining = int(cur.fetchone()[0])
        if remaining == 0:
            raise ExtractAoiMismatch(
                f"after clipping {clipped:,} features, this source has none "
                f"left inside AOI {aoi_id} — so the extract's bbox does not "
                f"overlap it. The usual cause is an extract taken with the "
                f"wrong --bbox: `overture extract` defaults to "
                f"MEXICO_CITY_BBOX, so omitting --bbox for any other city "
                f"silently extracts Mexico City. Check the bbox the parquet "
                f"was built with, then re-extract."
            )
    return {
        "clipped_outside_aoi": clipped,
        "clipped_to_aoi_id": aoi_id,
        # Recorded because a high-but-not-total clip is normal for a bbox that
        # crosses a border, and a reader needs to tell that from a near-miss.
        "clipped_fraction": (round(clipped / loaded, 4) if loaded else None),
        # The number the guard actually judges. Also the one that tells a
        # re-run (unchanged) from a first load (grew).
        "remaining_in_aoi": remaining,
    }


def validate_overture_buildings_layer(conn) -> dict[str, Any]:
    """Return a compact validation snapshot for the first spatial layer."""
    reader = backend_for_connection(conn).serving_reader()
    type_slug = OVERTURE_BUILDINGS_MEXICO_CITY.feature_type_slug
    count = reader.count_features(type_slug=type_slug)
    page = reader.get_features(type_slug=type_slug, limit=3)
    samples = [
        {
            "source_record_ref": row.get("name"),
            "attributes": row.get("attributes"),
        }
        for row in page["features"]
    ]
    return {
        "count": count,
        "sample_features": samples,
        "queryable_through_serving": count >= 0,
        "type_slug": type_slug,
    }
