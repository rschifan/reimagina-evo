"""Raster zonal-stats worker — compute per-feature stats from a COG.

Reads one registered raster source (``curated_geo.raster_layer`` + its
``file_asset``) and writes one row per (feature, statistic) into
``curated_geo.observation``. The worker is the R3 deliverable in the
raster-integration playbook.

Design-doc rules encoded here (Rev 7):

- The worker reprojects feature geometries to the raster's native CRS
  explicitly via ``rasterio.warp.transform_geom``. rasterstats does NOT
  reproject internally — silently mis-aligned inputs would return all
  nodata. Failing loudly at the edge is cheaper than debugging a
  misleading result later.
- Default statistic set depends on the layer's ``aggregation_kind``:
  intensive rasters (temperature, NDVI) get mean/median/p05/p95/std;
  extensive rasters (population count, area) get sum/count.
- The retire + insert critical section is guarded by a prefix lock:
  a transaction-scoped ``pg_try_advisory_xact_lock`` on a dedicated
  connection so the lock survives pooled callers.
- Lock contention raises :class:`ConcurrentRasterRun` — a dedicated
  exception so dashboards can filter on the lock-miss error code.
- The ``ingestion_run`` row is committed **before** the critical
  section, so a crash in retire/insert still leaves a well-formed
  ``failed`` run row rather than an orphan ``running`` row.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import math

import exactextract
import geopandas as gpd
import rasterio
from rasterio.warp import transform_geom
from shapely import wkb as shp_wkb
from shapely.geometry import shape as shapely_shape

from reimagina.data import catalog as cat
from reimagina.storage import backend_for_connection, connect
from reimagina.tiles.defaults import to_host_path


logger = logging.getLogger(__name__)


def _finalise_run_safely(conn, run_id: str, *, status: str, details: dict) -> None:
    """Call ``cat.complete_ingestion_run`` and swallow any exception.

    Used on BOTH failure and success paths: observations are committed
    to their dedicated transaction before this runs, so a flaky status
    write (e.g. MV refresh error, aborted pooled connection) must not
    leave the caller with half-applied state. On failure paths the
    unmasked original exception still propagates from the caller; on
    the success path the finalise error is the only thing logged — the
    run row may stay ``running`` in that rare case, but the observation
    writes are already durable so operators still see the real data.
    """
    try:
        cat.complete_ingestion_run(conn, run_id, status=status, details=details)
        _maybe_commit(conn)
    except Exception as finalise_exc:
        logger.warning(
            "failed to mark ingestion_run %s as %s; "
            "row may remain in running state. Finalise error: %s",
            run_id,
            status,
            finalise_exc,
        )


class ConcurrentRasterRun(RuntimeError):
    """Raised when another run already holds the prefix lock.

    Expected failure mode under parallel invocation — callers (CLI,
    worker queue) can retry or surface the collision to the user. The
    ingestion_run row is marked ``failed`` with
    ``details.error_code='ConcurrentRasterRun'`` so dashboards can
    filter on it.
    """


# Map worker-facing short names to ``(op_input, op_output)`` for
# exactextract. ``op_input`` is what we pass to ``exact_extract(...,
# ops=[...])``; ``op_output`` is the column name exactextract emits in
# the result DataFrame. They differ for quantile-family stats —
# exactextract names quantile columns ``quantile_<percent>`` (e.g.
# ``quantile_5``) regardless of the input syntax.
_STAT_TO_OP: dict[str, tuple[str, str]] = {
    "p05": ("quantile(q=0.05)", "quantile_5"),
    "p95": ("quantile(q=0.95)", "quantile_95"),
    "median": ("quantile(q=0.5)", "quantile_50"),
    # exactextract names the standard deviation ``stdev`` (input + output).
    "std": ("stdev", "stdev"),
}


def _exactextract_op(stat: str) -> str:
    """Translate a worker-facing stat name to its exactextract input op-string."""
    return _STAT_TO_OP.get(stat, (stat, stat))[0]


def _exactextract_output_col(stat: str) -> str:
    """Translate a worker-facing stat name to its exactextract output column name."""
    return _STAT_TO_OP.get(stat, (stat, stat))[1]


# Default statistic set per aggregation_kind. Extensive rasters (counts,
# areas, populations) make sense to ``sum``; intensive rasters
# (temperature, NDVI) only make sense to aggregate via central tendency
# or dispersion. The CHECK constraint on ``raster_layer.aggregation_kind``
# gates these two keys at the DB layer.
_DEFAULT_STATS_BY_KIND: dict[str, tuple[str, ...]] = {
    "intensive": ("mean", "median", "p05", "p95", "std"),
    "extensive": ("sum", "count"),
}


def _prefix_lock_key(prefix: str) -> int:
    """Derive the ``pg_try_advisory_xact_lock`` key for a prefix.

    Uses blake2b for stdlib availability + a ``reimagina.raster.``
    namespace salt to avoid collisions with any other advisory-lock
    user in the same database. Byte order + signedness are pinned
    explicitly (Codex Rev-5 review new-defect #4):
    ``pg_try_advisory_xact_lock(bigint)`` accepts the full int64 range
    including negatives, so ``signed=True`` is fine.
    """
    digest = hashlib.blake2b(
        b"reimagina.raster." + prefix.encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@dataclass(frozen=True)
class ZonalStatsResult:
    """Outcome of a single :func:`run_raster_zonal_stats` call."""

    source_id: str
    ingestion_run_id: str
    attribute_key_prefix: str
    features_in_scope: int
    features_written: int
    observations_written: int


def run_raster_zonal_stats(
    conn,
    *,
    source_id: str,
    feature_type_slug: str,
    attribute_key_prefix: str,
    aoi_id: str | None = None,
    coverage_threshold: float = 0.0,
    statistics: tuple[str, ...] | None = None,
    code_version: str = "dev",
    direct_dsn: str | None = None,
) -> ZonalStatsResult:
    """Compute zonal statistics for every feature of a type against a raster.

    The ``conn`` argument is used to write the ``ingestion_run`` row
    (committed before the critical section). The retire + bulk_upsert
    pair runs on a dedicated connection with a transaction-scoped
    advisory lock — the DSN comes from ``direct_dsn`` or the
    ``DATABASE_URL_DIRECT`` env var (same direct-port connection Martin
    and the migration runner use).

    ``statistics`` defaults to the kind-appropriate set from
    :data:`_DEFAULT_STATS_BY_KIND`. Callers override only for the
    pathological case where an intensive raster's default set is too
    wide (or too narrow).

    ``coverage_threshold`` is the **fractional valid-pixel count**
    below which a feature is skipped (no observation rows written).
    Default 0.0 — write a row for every polygon that intersects any
    non-nodata cell; under exactextract's area-weighting that's the
    cheap, conservative answer. Raise it (e.g. 0.5) only when callers
    explicitly want to exclude polygons whose intersection with the
    raster is too partial to be trusted; the previous (pre-exactextract)
    default of 0.5 silently dropped manzanas smaller than the raster
    pixel and is no longer recommended.

    Raises :class:`ConcurrentRasterRun` if another run already holds
    the prefix lock. Raises :class:`RuntimeError` for missing
    file_asset / raster_layer rows.
    """
    backend = backend_for_connection(conn)
    asset = backend.file_asset_repo().get_by_source(source_id)
    if asset is None:
        raise RuntimeError(f"No file_asset for source_id={source_id!r}")
    layer = backend.raster_layer_repo().get_by_source(source_id)
    if layer is None:
        raise RuntimeError(f"No raster_layer for source_id={source_id!r}")

    aggregation_kind = layer["aggregation_kind"]
    if statistics is None:
        statistics = _DEFAULT_STATS_BY_KIND[aggregation_kind]
    raster_path = to_host_path(asset["storage_uri"])

    feature_type_id = backend.feature_repo().get_type_id("MEX", feature_type_slug)

    # --- Transaction A: ingestion_run row, committed before the
    # critical section begins so a crash below leaves a well-formed
    # failed-run row behind.
    ingestion_run_id = cat.start_ingestion_run(
        conn,
        source_id=source_id,
        orchestrator="worker",
        orchestrator_run_id=str(uuid4()),
        transformation_step="raster_zonal_stats",
        code_version=code_version,
    )
    _maybe_commit(conn)

    # Collect zonal-stats records outside the critical section. The
    # rasterio + rasterstats work is the slow part, and holding the
    # prefix lock across it would starve concurrent runs for no gain.
    # Trade-off: features inserted between this compute pass and the
    # retire step below will not contribute records on this run, and
    # will have any previously-written observations vacuously retired.
    # At v1 scale this is acceptable — the rerun either picks them up
    # or the operator runs a targeted re-ingest. Tightening to a
    # lock-held iteration would require re-architecting the worker
    # around streaming geometry cursors.
    try:
        records, features_in_scope, features_skipped_low_coverage = (
            _compute_zonal_stats_records(
                backend=backend,
                feature_type_id=feature_type_id,
                aoi_id=aoi_id,
                raster_path=str(raster_path),
                asset_id=asset["id"],
                attribute_key_prefix=attribute_key_prefix,
                statistics=statistics,
                coverage_threshold=coverage_threshold,
            )
        )
    except Exception as exc:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "RasterZonalStatsError",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise

    # --- Transaction B: critical section (retire + bulk insert) under
    # the prefix lock. Dedicated connection on Postgres so the
    # transaction-scoped advisory lock survives pooled callers.
    try:
        _run_critical_section_postgres(
            direct_dsn=direct_dsn,
            attribute_key_prefix=attribute_key_prefix,
            records=records,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
            # The scope this run actually recomputed: one grain, optionally one
            # AOI. Retirement is restricted to it, so a block-grain run cannot
            # retire the same prefix's AGEB values and an AOI-scoped run cannot
            # retire everything outside its AOI.
            feature_type_id=feature_type_id,
            aoi_id=aoi_id,
        )
    except ConcurrentRasterRun:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "ConcurrentRasterRun",
                "prefix": attribute_key_prefix,
            },
        )
        raise
    except Exception as exc:
        _finalise_run_safely(
            conn,
            ingestion_run_id,
            status="failed",
            details={
                "error_code": "RasterZonalStatsError",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise

    # Observations are already committed (Transaction B, dedicated conn
    # on Postgres). Finalise the run row through the same safe path as
    # the failure branches so a flaky MV refresh or status write can't
    # leave the run row in 'running' while the observations are live.
    _finalise_run_safely(
        conn,
        ingestion_run_id,
        status="succeeded",
        details={
            "features_in_scope": features_in_scope,
            "features_skipped_low_coverage": features_skipped_low_coverage,
            "observations_written": len(records),
            "statistics": list(statistics),
            "aggregation_kind": aggregation_kind,
        },
    )

    features_written = len({r["feature_id"] for r in records})
    return ZonalStatsResult(
        source_id=source_id,
        ingestion_run_id=ingestion_run_id,
        attribute_key_prefix=attribute_key_prefix,
        features_in_scope=features_in_scope,
        features_written=features_written,
        observations_written=len(records),
    )


def _compute_zonal_stats_records(
    *,
    backend,
    feature_type_id: str,
    aoi_id: str | None,
    raster_path: str,
    asset_id: str,
    attribute_key_prefix: str,
    statistics: tuple[str, ...],
    coverage_threshold: float,
) -> tuple[list[dict[str, Any]], int, int]:
    """Open the raster, run area-weighted zonal stats over every
    in-scope feature, produce observation records.

    Implementation notes
    --------------------
    Uses :mod:`exactextract` for the aggregation. Unlike rasterstats'
    centroid-in-polygon policy (which silently dropped manzanas
    smaller than the WorldPop 100 m pixel), exactextract gives every
    pixel a fractional weight equal to ``overlap_area / pixel_area``
    per polygon. That makes ``sum`` over an extensive raster (e.g.
    population) numerically conservative — i.e. the sum of per-block
    sums equals the raster total within float tolerance.

    The fractional weights also let us redefine ``coverage_threshold``
    cleanly: the ``count`` op returns the **fractional valid-pixel
    count** for the polygon (= 0 if the polygon doesn't intersect any
    non-nodata cell, > 0 otherwise). Defaulting the threshold to 0.0
    means we write a row for every polygon that overlaps the raster
    extent; a polygon entirely off the raster contributes nothing to
    the observation table at all.

    Returns ``(records, features_in_scope, features_skipped_low_coverage)``.
    No DB writes happen here — the caller owns the critical section.
    """
    # Map every requested user-facing stat name to its exactextract
    # ``(input op, output column)`` pair. Stored separately so the
    # quantile-family stats — whose input syntax (``quantile(q=…)``)
    # differs from their output column name (``quantile_<percent>``) —
    # don't desync silently.
    output_col_for_user: dict[str, str] = {
        s: _exactextract_output_col(s) for s in statistics
    }
    requested_ops = [_exactextract_op(s) for s in statistics]
    # ``count`` (fractional valid-pixel sum) is always pulled — it's
    # the gate the threshold check uses below. Avoid duplicates if the
    # caller already asked for it.
    if "count" not in requested_ops:
        requested_ops.append("count")

    feat_ids: list[str] = []
    geoms: list[Any] = []  # shapely geometries in the raster's CRS
    with rasterio.open(raster_path) as rast:
        target_crs = rast.crs.to_string()
        feature_rows = backend.feature_repo().iter_geometries_by_type(
            type_id=feature_type_id,
            aoi_id=aoi_id,
        )
        for feature_id, wkb_bytes in feature_rows:
            geom_4326 = shp_wkb.loads(bytes(wkb_bytes))
            geom_native_dict = transform_geom(
                "EPSG:4326",
                target_crs,
                geom_4326.__geo_interface__,
            )
            geoms.append(shapely_shape(geom_native_dict))
            feat_ids.append(feature_id)

    features_in_scope = len(geoms)
    if features_in_scope == 0:
        return [], 0, 0

    # Single batched call across all polygons. exactextract is built
    # for this — feeding it one geometry at a time would re-open the
    # raster and re-run grid-coverage setup for every iteration.
    gdf = gpd.GeoDataFrame(
        {"feature_id": feat_ids},
        geometry=geoms,
        crs=target_crs,
    )
    df = exactextract.exact_extract(
        raster_path,
        gdf,
        requested_ops,
        include_cols=["feature_id"],
        output="pandas",
    )

    records: list[dict[str, Any]] = []
    features_skipped_low_coverage = 0
    for _, row in df.iterrows():
        coverage = row.get("count")
        coverage = 0.0 if coverage is None or _is_nan(coverage) else float(coverage)
        if coverage <= 0.0 or coverage < coverage_threshold:
            features_skipped_low_coverage += 1
            continue
        for user_stat in statistics:
            value = row.get(output_col_for_user[user_stat])
            if value is None or _is_nan(value):
                continue
            records.append(
                {
                    "feature_id": row["feature_id"],
                    "attribute_key": f"{attribute_key_prefix}_{user_stat}",
                    "value": float(value),
                    "value_type": "numeric",
                    "lineage_ref": {
                        "kind": "raster_zonal_stats",
                        "asset_id": asset_id,
                        "stat_name": user_stat,
                        "fractional_pixel_count": coverage,
                    },
                }
            )

    return records, features_in_scope, features_skipped_low_coverage


def _is_nan(value: object) -> bool:
    """``math.isnan`` only accepts floats; pandas may hand us a numpy
    scalar or a Python None inside a DataFrame cell. This wrapper
    accepts either and returns False on non-numeric types so callers
    don't have to litter the read path with try/except."""
    try:
        return bool(math.isnan(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _run_critical_section_postgres(
    *,
    direct_dsn: str | None,
    attribute_key_prefix: str,
    records: list[dict[str, Any]],
    source_id: str,
    ingestion_run_id: str,
    feature_type_id: str,
    aoi_id: str | None = None,
) -> None:
    """Retire the prefix and write the new rows, under the prefix lock.

    ``feature_type_id`` and ``aoi_id`` describe **what this run recomputed**,
    and scoping the retire to them is load-bearing rather than tidy.
    ``bulk_upsert_current`` already retires per ``(feature_id, attribute_key)``,
    so the prefix-wide retire exists only to catch rows the new run *no longer
    writes* — a feature that dropped below the coverage threshold, a statistic
    no longer produced. Its scope therefore has to be exactly the run's scope.
    Unscoped, it also caught every other grain's and every other AOI's rows,
    retired them, and re-inserted nothing in their place.
    """
    dsn = direct_dsn or os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError(
            "Postgres zonal-stats worker requires a direct DSN "
            "(pass direct_dsn=... or set DATABASE_URL_DIRECT)"
        )
    tx_conn = connect(dsn)
    try:
        tx_conn.autocommit = False
        with tx_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (_prefix_lock_key(attribute_key_prefix),),
            )
            row = cur.fetchone()
            # Postgres always returns a bool here; defensively guard against
            # NULL / missing row so a stub cursor can't silently let us run
            # un-locked.
            locked = row is not None and row[0] is True
        if not locked:
            tx_conn.rollback()
            raise ConcurrentRasterRun(attribute_key_prefix)
        tx_backend = backend_for_connection(tx_conn)
        tx_backend.observation_repo().retire_current_by_prefix(
            prefix=attribute_key_prefix,
            feature_type_id=feature_type_id,
            aoi_id=aoi_id,
        )
        tx_backend.observation_repo().bulk_upsert_current(
            records,
            source_id=source_id,
            ingestion_run_id=ingestion_run_id,
        )
        tx_conn.commit()
    except ConcurrentRasterRun:
        raise
    except Exception:
        try:
            tx_conn.rollback()
        except Exception:
            pass
        raise
    finally:
        tx_conn.close()


def _maybe_commit(conn) -> None:
    """Commit unless the caller is already in autocommit mode.

    Postgres callers in autocommit mode raise when ``.commit()`` is
    called while no transaction is open, so we guard with the autocommit
    flag.
    """
    if getattr(conn, "autocommit", False):
        return
    conn.commit()


__all__ = [
    "ConcurrentRasterRun",
    "ZonalStatsResult",
    "run_raster_zonal_stats",
]
