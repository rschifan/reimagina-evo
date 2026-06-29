"""CLI helpers for the raster ingest pipeline (R2) + zonal stats (R3).

Subcommands:
    reimagina raster fetch-worldpop — download (cached) + optionally clip
                                      a WorldPop country raster, ready
                                      to feed into ``raster ingest``
    reimagina raster ingest         — land + stage + register a single-band raster
    reimagina raster list           — dump the engineer-inspection view of registered layers
    reimagina raster publish        — flip the published flag on a layer_slug
    reimagina raster zonal-stats    — write per-feature stats from a raster to observations

Kept as thin argparse wrappers over the pipeline / repo / worker calls
— all actual logic lives in reimagina.pipeline.raster,
reimagina.pipeline.worldpop, reimagina.jobs.raster_zonal_stats, and
the R1 storage surface. The CLI's job is parsing flags + serialising
output as JSON.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from reimagina.db import connect
from reimagina.jobs.raster_zonal_stats import (
    ConcurrentRasterRun,
    run_raster_zonal_stats,
)
from reimagina.pipeline import worldpop as worldpop_pipeline
from reimagina.pipeline.raster import ingest_raster_source
from reimagina.storage import backend_for_connection


logger = logging.getLogger(__name__)


class PrefixCollisionError(RuntimeError):
    """Raised when another source already owns an observation prefix.

    The SCD invariant on ``curated_geo.observation`` is per
    ``(feature_id, attribute_key)`` regardless of source — two sources
    writing under the same prefix would silently retire each other's
    rows on every run. Refusing at CLI entry is cheaper than debugging
    a mysteriously empty observation snapshot.
    """


def ingest_raster(
    *,
    dsn: str,
    raw_path: str | Path,
    source_name: str,
    provider: str,
    version: str,
    layer_slug: str,
    aggregation_kind: str = "intensive",
    logical_name: str | None = None,
    description: str | None = None,
    colormap_name: str | None = None,
    rescale: str | None = None,
    license: str | None = None,
    captured_at: str | None = None,
    minzoom: int = 5,
    maxzoom: int = 14,
    published: bool = True,
    code_version: str = "dev",
    orchestrator_run_id: str | None = None,
) -> dict[str, Any]:
    """Run ingest_raster_source against the given DSN, return JSON-friendly result."""
    captured_at_dt: datetime | None = None
    if captured_at:
        # Accept ISO-8601 with or without timezone; let datetime raise on malformed.
        captured_at_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    with connect(dsn) as conn:
        result = ingest_raster_source(
            conn,
            raw_path=Path(raw_path),
            source_name=source_name,
            provider=provider,
            version=version,
            layer_slug=layer_slug,
            aggregation_kind=aggregation_kind,
            logical_name=logical_name,
            description=description,
            colormap_name=colormap_name,
            rescale=rescale,
            license=license,
            captured_at=captured_at_dt,
            minzoom=minzoom,
            maxzoom=maxzoom,
            published=published,
            code_version=code_version,
            orchestrator_run_id=orchestrator_run_id,
        )
    return {
        "source_id": result.source_id,
        "asset_id": result.asset_id,
        "ingestion_run_id": result.ingestion_run_id,
        "layer_id": result.layer_id,
        "layer_slug": layer_slug,
        "silver_path": str(result.silver_path),
        "storage_uri": result.storage_uri,
        "crs": result.crs,
        "resolution_m": result.resolution_m,
    }


def fetch_worldpop_raster(
    *,
    country: str,
    year: int,
    output_path: str | Path,
    cache_dir: str | Path | None = None,
    dataset: str = worldpop_pipeline.DEFAULT_DATASET,
    aoi_id: str | None = None,
    dsn: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Resolve a WorldPop country raster from the public REST API,
    optionally clip it to an AOI polygon, and write the result to
    ``output_path``.

    The output of this helper is exactly what ``raster ingest`` expects
    via ``--raw-path`` — no post-processing required between the two
    steps. We deliberately keep the country-scale fetch + the AOI clip
    inside this CLI helper (not in the lower-level pipeline module) so
    the DB lookup for the AOI polygon stays in the CLI layer where
    the connection lifecycle is already managed.

    ``aoi_id`` is required to perform the clip; without it the country
    raster (~1 GB for Mexico) is copied through verbatim.
    """
    aoi_geometry_wkt: str | None = None
    if aoi_id is not None:
        if dsn is None:
            raise ValueError(
                "fetch_worldpop_raster: dsn is required when aoi_id is given "
                "— the AOI polygon is read from curated_geo.area_of_interest"
            )
        # The AOI repo's ``get_aoi`` deliberately omits geometry to keep
        # the JSON response slim for the web app. For clipping we need
        # WKT, so query it directly here. ``ST_AsText(geometry)`` is
        # cheap (single row, indexed by uuid) and the CLI is the only
        # caller that needs the full polygon.
        with connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, ST_AsText(geometry) "
                    "FROM curated_geo.area_of_interest WHERE id = %s",
                    (aoi_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"fetch_worldpop_raster: AOI id {aoi_id!r} not found in "
                f"curated_geo.area_of_interest. Run `aoi list` to confirm "
                f"the uuid."
            )
        _aoi_name, aoi_geometry_wkt = row
        if not aoi_geometry_wkt:
            raise ValueError(
                f"AOI {aoi_id!r} has no geometry — clipping requires a "
                f"polygon. Inspect curated_geo.area_of_interest.geometry "
                f"for this row."
            )

    return worldpop_pipeline.fetch(
        country=country,
        year=year,
        output_path=output_path,
        cache_dir=cache_dir,
        dataset=dataset,
        aoi_geometry_wkt=aoi_geometry_wkt,
        force=force,
    )


def list_raster_layers(*, dsn: str) -> dict[str, Any]:
    """List every raster layer registered in the DB.

    Reads ``explore.raster_layer_v`` for operator-facing inspection —
    includes source name + version + storage URI alongside the layer
    slug and publish status.
    """
    with connect(dsn) as conn:
        with _cursor_compat(conn) as cur:
            # Both backends define explore.raster_layer_v.
            cur.execute(
                """
                SELECT layer_slug, published, aggregation_kind,
                       colormap_name, rescale, minzoom, maxzoom,
                       source_name, source_version,
                       format, crs, resolution_m, storage_uri
                FROM explore.raster_layer_v
                ORDER BY layer_slug
                """
            )
            rows = cur.fetchall()
            # cursor.description gives us the column list for both engines.
            cols = [d[0] for d in cur.description]
    return {
        "dsn": dsn,
        "layers": [dict(zip(cols, row, strict=False)) for row in rows],
    }


def run_zonal_stats(
    *,
    dsn: str,
    source_id: str,
    feature_type_slug: str,
    attribute_key_prefix: str,
    aoi_id: str | None = None,
    coverage_threshold: float = 0.0,
    statistics: str | None = None,
    code_version: str = "dev",
    force: bool = False,
    direct_dsn: str | None = None,
) -> dict[str, Any]:
    """Compute zonal stats for a raster against features of a type.

    Thin wrapper over :func:`run_raster_zonal_stats` that adds the two
    CLI-only concerns the worker itself stays out of:

    - **Prefix-collision check**: calls ``has_active_prefix`` on the
      observation repo with ``excluding_source_id=source_id``. If
      another source already owns current observations under this
      prefix, refuses the run unless ``force=True`` — preventing two
      sources from silently retiring each other's rows.
    - **Small-feature warning**: for extensive aggregations against a
      coarse raster, emits a warning when the equivalent-circle
      diameter of the median feature is less than ``3 × resolution_m``.
      That's the boundary-bias regime where ``rasterstats`` with
      ``all_touched=False`` systematically under-counts; see the R7
      (``exactextract``) deferral note in the design doc.
    """
    stat_tuple = _parse_statistics(statistics)

    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        # Read layer + asset metadata up front so we can build the
        # small-feature warning check before spending time in the worker.
        layer = backend.raster_layer_repo().get_by_source(source_id)
        if layer is None:
            raise RuntimeError(f"No raster_layer for source_id={source_id!r}")

        # Prefix-collision check (CLI-only; the worker itself doesn't
        # enforce this because internal callers may legitimately share
        # a prefix across ingestion runs of the same source).
        if not force:
            collides = backend.observation_repo().has_active_prefix(
                prefix=attribute_key_prefix,
                excluding_source_id=source_id,
            )
            if collides:
                raise PrefixCollisionError(
                    f"Attribute-key prefix {attribute_key_prefix!r} is already "
                    "owned by another source's current observations. Either "
                    "pick a different prefix, retire the other source's rows "
                    "first, or re-run with --force if you have verified the "
                    "collision is intentional."
                )

        small_feature_warning = _check_small_feature_warning(
            backend=backend,
            layer=layer,
            feature_type_slug=feature_type_slug,
            aoi_id=aoi_id,
        )

        try:
            result = run_raster_zonal_stats(
                conn,
                source_id=source_id,
                feature_type_slug=feature_type_slug,
                attribute_key_prefix=attribute_key_prefix,
                aoi_id=aoi_id,
                coverage_threshold=coverage_threshold,
                statistics=stat_tuple,
                code_version=code_version,
                direct_dsn=direct_dsn,
            )
        except ConcurrentRasterRun as exc:
            # Re-raise with a CLI-friendly message. The caller
            # typically retries after a short delay.
            raise RuntimeError(
                f"Another zonal-stats run holds the lock for prefix "
                f"{exc.args[0]!r}. Wait for it to finish and re-run."
            ) from exc

    out: dict[str, Any] = {
        "source_id": result.source_id,
        "ingestion_run_id": result.ingestion_run_id,
        "attribute_key_prefix": result.attribute_key_prefix,
        "features_in_scope": result.features_in_scope,
        "features_written": result.features_written,
        "observations_written": result.observations_written,
    }
    if small_feature_warning is not None:
        out["warnings"] = [small_feature_warning]
    return out


def _parse_statistics(statistics: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated list of stat names, or return None for default."""
    if statistics is None:
        return None
    parts = [s.strip() for s in statistics.split(",") if s.strip()]
    if not parts:
        return None
    return tuple(parts)


def _check_small_feature_warning(
    *,
    backend,
    layer: dict[str, Any],
    feature_type_slug: str,
    aoi_id: str | None,
) -> str | None:
    """Return a warning string if most features are smaller than 3×
    the raster's pixel size — or ``None`` when the warning doesn't
    apply (non-extensive aggregation, unknown resolution, or no
    features of the type).

    The check uses the median feature's equivalent-circle diameter
    (``2·√(area/π)``). Median rather than mean because urban feature
    distributions (blocks, lots) are typically right-skewed — a handful
    of very large blocks would pull the mean well above the threshold
    and silence the warning on the common case.
    """
    aggregation_kind = layer.get("aggregation_kind")
    resolution_m = layer.get("resolution_m")
    # Only fires for extensive aggregations against a resolution-known
    # raster; intensive stats aren't subject to the boundary-bias
    # problem this warning catches.
    if aggregation_kind != "extensive" or resolution_m is None:
        return None
    try:
        type_id = backend.feature_repo().get_type_id("MEX", feature_type_slug)
    except ValueError:
        # Unknown slug — skip the advisory warning. DB/connectivity errors are
        # left to propagate (the real run hits them too; masking them here hid
        # genuine failures).
        return None
    metrics = backend.feature_repo().geodesic_metrics_by_type(
        type_id=type_id,
        aoi_id=aoi_id,
    )
    if not metrics:
        return None
    areas = [float(row[1]) for row in metrics if row[1] is not None]
    if not areas:
        return None
    median_area = median(areas)
    equiv_diameter = 2.0 * math.sqrt(median_area / math.pi)
    threshold = 3.0 * float(resolution_m)
    if equiv_diameter < threshold:
        return (
            f"small-feature warning: median {feature_type_slug} has "
            f"equivalent-circle diameter {equiv_diameter:.1f} m, "
            f"below 3× the raster resolution ({threshold:.1f} m). "
            "Extensive aggregation under all_touched=False will "
            "systematically under-count; see the R7 exactextract "
            "deferral in docs/architecture/TODO.md."
        )
    return None


def set_raster_published(
    *, dsn: str, layer_slug: str, published: bool
) -> dict[str, Any]:
    """Flip the published flag on a raster_layer row."""
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        backend.raster_layer_repo().set_published(
            layer_slug=layer_slug,
            published=published,
        )
    return {
        "dsn": dsn,
        "layer_slug": layer_slug,
        "published": published,
    }


# ── Aggregation recipes (r020) ─────────────────────────────────────────


def add_derivation(
    *,
    dsn: str,
    input_kind: str,
    target_feature_type: str,
    attribute_key_prefix: str,
    method: str,
    raster_layer_slug: str | None = None,
    input_prefix: str | None = None,
    input_feature_type: str | None = None,
    weight_basis: str | None = None,
    params_json: str | None = None,
    coverage_threshold: float = 0.5,
    aoi_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Register one aggregation recipe. Returns the new recipe row."""
    from reimagina.jobs.derivations import run_recipe  # noqa: F401  (surface import)

    params = json.loads(params_json) if params_json else None

    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        input_raster_layer_id: str | None = None
        if input_kind == "raster":
            if not raster_layer_slug:
                raise ValueError("input_kind='raster' requires --raster-layer-slug")
            # Resolve slug → layer id via the serving view.
            layer = _find_raster_layer_by_slug(backend, raster_layer_slug)
            if layer is None:
                raise ValueError(f"Unknown raster layer slug: {raster_layer_slug!r}")
            input_raster_layer_id = layer["id"]
        else:  # observation
            if not (input_prefix and input_feature_type):
                raise ValueError(
                    "input_kind='observation' requires --input-prefix and --input-feature-type"
                )

        recipe_id = backend.aggregation_recipe_repo().register_recipe(
            input_kind=input_kind,
            target_feature_type_slug=target_feature_type,
            attribute_key_prefix=attribute_key_prefix,
            method=method,
            input_raster_layer_id=input_raster_layer_id,
            input_attribute_key_prefix=input_prefix,
            input_feature_type_slug=input_feature_type,
            weight_basis=weight_basis,
            params=params,
            coverage_threshold=coverage_threshold,
            aoi_id=aoi_id,
            enabled=enabled,
        )
        conn.commit()
        row = backend.aggregation_recipe_repo().get_recipe(recipe_id)
    return {"dsn": dsn, "recipe": row}


def list_derivations(
    *,
    dsn: str,
    raster_layer_slug: str | None = None,
    include_disabled: bool = False,
) -> dict[str, Any]:
    """List recipes. Optionally filter by raster layer slug."""
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        layer_id: str | None = None
        if raster_layer_slug:
            layer = _find_raster_layer_by_slug(backend, raster_layer_slug)
            if layer is None:
                raise ValueError(f"Unknown raster layer slug: {raster_layer_slug!r}")
            layer_id = layer["id"]
        rows = backend.aggregation_recipe_repo().list_recipes(
            input_raster_layer_id=layer_id,
            enabled_only=not include_disabled,
        )
    return {"dsn": dsn, "recipes": rows}


def set_derivation_enabled(
    *, dsn: str, recipe_id: str, enabled: bool
) -> dict[str, Any]:
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        backend.aggregation_recipe_repo().set_enabled(
            recipe_id=recipe_id,
            enabled=enabled,
        )
        conn.commit()
    return {"dsn": dsn, "recipe_id": recipe_id, "enabled": enabled}


def remove_derivation(*, dsn: str, recipe_id: str) -> dict[str, Any]:
    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        backend.aggregation_recipe_repo().delete_recipe(recipe_id)
        conn.commit()
    return {"dsn": dsn, "recipe_id": recipe_id, "deleted": True}


def run_derivation(
    *,
    dsn: str,
    recipe_id: str | None = None,
    raster_layer_slug: str | None = None,
    direct_dsn: str | None = None,
) -> dict[str, Any]:
    """Run one recipe by id, OR every enabled recipe for a raster layer."""
    from reimagina.jobs.derivations import (
        RecipeRunResult,
        run_all_for_raster,
        run_recipe,
    )

    if not (recipe_id or raster_layer_slug):
        raise ValueError("Pass --recipe-id OR --raster-layer-slug")

    with connect(dsn) as conn:
        backend = backend_for_connection(conn)
        if recipe_id:
            results: list[RecipeRunResult] = [
                run_recipe(conn, recipe_id=recipe_id, direct_dsn=direct_dsn)
            ]
        else:
            layer = _find_raster_layer_by_slug(backend, raster_layer_slug)
            if layer is None:
                raise ValueError(f"Unknown raster layer slug: {raster_layer_slug!r}")
            results = run_all_for_raster(
                conn,
                raster_layer_id=layer["id"],
                direct_dsn=direct_dsn,
            )

    return {
        "dsn": dsn,
        "runs": [
            {
                "recipe_id": r.recipe_id,
                "input_kind": r.input_kind,
                "ingestion_run_id": r.ingestion_run_id,
                "attribute_key_prefix": r.attribute_key_prefix,
                "observations_written": r.observations_written,
            }
            for r in results
        ],
    }


def _find_raster_layer_by_slug(backend, layer_slug: str) -> dict | None:
    """Resolve a *published* raster ``layer_slug`` to its row, or ``None``.

    Scans the published list (cheap in v1 — a handful of rasters per
    deployment). Unpublished layers are not resolvable by slug here; callers
    pass a published layer (the common case)."""
    for row in backend.raster_layer_repo().list_published_layers():
        if row["layer_slug"] == layer_slug:
            return row
    return None


def as_json(data: dict[str, Any]) -> str:
    """Serialise CLI output consistently."""
    return json.dumps(data, indent=2, sort_keys=True, default=str)


class _cursor_compat:
    """Tiny context manager that yields a cursor-like object from either
    a psycopg2 connection (``.cursor()`` as a context manager) or a
    DuckDB connection (``.execute()`` on the connection itself).

    Reading a single explore view in both backends doesn't justify a
    new repo method; this thin shim keeps the CLI code readable
    without pushing the query into the storage layer.
    """

    def __init__(self, conn):
        self._conn = conn
        self._is_pg = hasattr(conn, "cursor")

    def __enter__(self):
        if self._is_pg:
            self._cur = self._conn.cursor()
            self._cur.__enter__()
            return self._cur
        # DuckDB connection doubles as a cursor; the .execute() call
        # returns the cursor-like relation we can pull description from.
        return _DuckCursorAdapter(self._conn)

    def __exit__(self, exc_type, exc, tb):
        if self._is_pg:
            return self._cur.__exit__(exc_type, exc, tb)
        return None


class _DuckCursorAdapter:
    """Shim that exposes a psycopg2-cursor-ish ``execute`` /
    ``fetchall`` / ``description`` surface over a DuckDB connection.
    DuckDB's ``execute`` returns a DuckDBPyRelation; we capture it so
    ``description`` is available after the fetch."""

    def __init__(self, conn):
        self._conn = conn
        self._rel = None

    def execute(self, sql: str, params: tuple = ()):
        self._rel = self._conn.execute(sql, list(params))
        return self

    def fetchall(self):
        return self._rel.fetchall()

    @property
    def description(self):
        return self._rel.description
