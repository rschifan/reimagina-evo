"""Operation-shaped storage interfaces for the data layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
from datetime import datetime

import geopandas as gpd


class SourceCatalogRepository(Protocol):
    def register_or_get_source(
        self,
        *,
        name: str,
        provider: str,
        storage_type: str,
        integration_role: str,
        storage_uri: str,
        version: str,
        license: str | None = None,
        notes: str | None = None,
    ) -> str: ...

    def get_source(self, *, name: str, provider: str, version: str) -> dict | None: ...

    def update_source_storage_uri(self, source_id: str, storage_uri: str) -> None: ...

    def start_ingestion_run(
        self,
        *,
        source_id: str,
        orchestrator: str,
        orchestrator_run_id: str,
        transformation_step: str,
        code_version: str,
    ) -> str: ...

    def complete_ingestion_run(
        self,
        run_id: str,
        *,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...


class FeatureRepository(Protocol):
    def seed_feature_types(self, rows: list[tuple[Any, ...]]) -> None: ...

    def get_type_id(self, context: str, type_slug: str) -> str: ...

    def bulk_insert_features(
        self,
        gdf: gpd.GeoDataFrame,
        *,
        source_id: str,
        ingestion_run_id: str,
        type_id: str,
        dataset_version: str,
        source_record_ref_col: str,
        name_col: str | None = None,
        attributes_cols: list[str] | None = None,
        parent_id_col: str | None = None,
    ) -> int: ...

    def bulk_insert_features_from_parquet(
        self,
        *,
        parquet_path: str | Path,
        source_id: str,
        ingestion_run_id: str,
        type_id: str,
        dataset_version: str,
        source_record_ref_col: str,
        name_col: str | None = None,
        attributes_cols: list[str] | None = None,
        parent_id_col: str | None = None,
    ) -> int:
        """Insert features directly from a GeoParquet file.

        Implementations should stream the file in bounded batches rather than
        materialising it fully in Python. The Postgres backend provides a native
        implementation; the loader falls back to the GeoDataFrame path if a
        backend omits this method.
        """
        ...

    def geodesic_metrics_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[tuple[str, float, float]]:
        """Return (feature_id, area_m2, perimeter_m) for every feature of a type.

        Postgres uses the ``geography`` cast for geodesic measurement. Scope
        with ``aoi_id`` to restrict to features intersecting
        an AOI, and/or ``feature_ids`` to restrict to a specific set (the
        selective-recompute path — only the dirty features). Both narrow the
        result and compose (AND) when given together.
        """
        ...

    def find_by_attributes(
        self,
        *,
        type_slug: str,
        attributes: dict[str, str],
    ) -> dict[str, str] | None:
        """Return ``{id, geometry_wkt}`` for the first feature whose ``type_slug``
        matches and whose ``attributes`` JSON contains the given key/value pairs
        exactly, or ``None``. Attribute values are compared as strings — the
        concrete backends use the relevant JSON-access function per engine.
        """
        ...

    def iter_geometries_by_type(
        self,
        *,
        type_id: str,
        aoi_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return ``(feature_id, WKB bytes EPSG:4326)`` pairs for every
        feature of a type. ``aoi_id`` **filters** to features that
        intersect the AOI — no clipping, callers receive whole feature
        geometries.

        The method is named ``iter_*`` for caller-side readability, but
        returns a materialised list for consistency with the sibling
        by-type methods; cold-path raster zonal stats process at most
        66k CDMX blocks × ~100 bytes WKB ≈ 6 MB, which is cheaper than
        the plumbing cost of a streaming generator.

        Earns its place by giving callers a single geometry-fetch entry
        point (Postgres UUID→text, PostGIS ST_AsBinary) instead of inlining
        the SQL at every call site.
        """
        ...

    def centroid_in_parent_counts(
        self,
        *,
        parent_type_id: str,
        child_type_id: str,
        aoi_id: str | None = None,
        feature_ids: list[str] | None = None,
    ) -> list[tuple[str, int, float]]:
        """Return (parent_feature_id, child_count, area_m2) for every parent.

        Counts children whose centroid is inside each parent (the standard
        point-in-polygon attribution rule that avoids double-counting at
        boundaries). LEFT-joined so parents with zero matching children
        still appear — callers can distinguish 'measured zero' from 'not
        observed'. Area uses the same units/convention as
        ``geodesic_metrics_by_type`` on the same backend. Scope with
        ``aoi_id`` and/or ``feature_ids`` (restricting the *parents* to the
        selective-recompute set); both compose (AND).
        """
        ...


class ObservationRepository(Protocol):
    def upsert_current(
        self,
        *,
        feature_id: str,
        attribute_key: str,
        value_type: str,
        value: Any,
        source_id: str,
        ingestion_run_id: str,
        lineage_ref: dict | None = None,
    ) -> str: ...

    def bulk_upsert_current(
        self,
        records: list[dict[str, Any]],
        *,
        source_id: str,
        ingestion_run_id: str,
    ) -> int: ...

    def retire_current_by_prefix(self, *, prefix: str) -> int:
        """Retire every current observation whose ``attribute_key`` either
        equals ``:prefix`` exactly or starts with ``:prefix_`` (literal
        separator, NOT a SQL LIKE wildcard).

        Implementations use ``starts_with(attribute_key, :prefix || '_')``
        so ``prefix='ndvi'`` does not accidentally retire ``ndvi2_mean``.
        Retirement is cross-source by design — the ``obs_one_current``
        SCD invariant is per ``(feature_id, attribute_key)`` regardless
        of source, and retiring only one source's rows would leave the
        invariant broken. Callers that want per-source scoping should
        enforce single-source prefix ownership via
        ``has_active_prefix`` before calling this.

        Raises ``ValueError`` on empty prefix to prevent catch-all
        retirement. Returns the number of rows transitioned to
        ``is_current = false``.
        """
        ...

    def has_active_prefix(
        self,
        *,
        prefix: str,
        excluding_source_id: str,
    ) -> bool:
        """Return True iff any ``curated_geo.observation`` row exists with
        ``is_current = true``, ``source_id != :excluding_source_id``,
        and an ``attribute_key`` equal to ``:prefix`` or matching
        ``:prefix_<suffix>`` under the same separator-safe pattern as
        ``retire_current_by_prefix``.

        No "active source" concept — what matters for SCD safety is
        whether some other source's current observation already owns
        the prefix, because the writer retires/upserts on
        ``(feature_id, attribute_key)`` regardless of source. Published
        status is irrelevant. Backs the CLI prefix-collision check.
        """
        ...


class FileAssetRepository(Protocol):
    def register_file_asset(
        self,
        *,
        source_id: str,
        ingestion_run_id: str,
        storage_uri: str,
        dataset_version: str,
        format: str | None = None,
        crs: str | None = None,
        resolution_m: float | None = None,
        band_description: dict | None = None,
        captured_at: Any | None = None,
    ) -> str: ...

    def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        """Return the ``file_asset`` row for a source_id, or None.

        Earns its place by centralising the lookup SQL — otherwise every
        caller writes its own ``SELECT ... WHERE source_id = %s`` with the
        RealDictCursor row handling. Same rationale as
        ``iter_geometries_by_type``.
        """
        ...


class RasterLayerRepository(Protocol):
    """Registry of raster tile layers — one row per published raster.

    ``curated_geo.raster_layer`` is the canonical write target (r019);
    read-side methods go through ``serving_geo.raster_layer_v`` so
    workers honour the "API/workers read only serving_geo" rule.

    Three real consumers earn the Protocol's place: the R2 ingest
    pipeline (``register_layer``), the R3 zonal-stats worker
    (``get_by_source`` to read ``aggregation_kind``), and the R4 tile-
    source resolver (``list_published_layers``).
    """

    def register_layer(
        self,
        *,
        source_id: str,
        asset_id: str,
        layer_slug: str,
        logical_name: str | None = None,
        description: str | None = None,
        colormap_name: str | None = None,
        rescale: str | None = None,
        minzoom: int = 5,
        maxzoom: int = 14,
        published: bool = True,
        aggregation_kind: str = "intensive",
    ) -> str:
        """Insert a new ``curated_geo.raster_layer`` row. Returns the
        new row's id. ``aggregation_kind`` must be one of
        ``{'intensive', 'extensive'}`` (enforced by the DB CHECK).
        """
        ...

    def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        """Return the layer row for a source_id (read from
        ``serving_geo.raster_layer_v``), or None. Used by the zonal-
        stats worker to read ``aggregation_kind`` + asset metadata
        (``storage_uri``, ``crs``, ``resolution_m``).
        """
        ...

    def get_by_id(self, layer_id: str) -> dict[str, Any] | None:
        """Return the layer row for a raster_layer id (read from
        ``serving_geo.raster_layer_v``), or None. Used by the recipe
        dispatcher to translate a recipe's ``input_raster_layer_id``
        (UI-picked) into the ``source_id`` the compute worker wants.
        ``get_by_source`` is the wrong shape here: a recipe binds to
        the layer, not the source.
        """
        ...

    def list_published_layers(self) -> list[dict[str, Any]]:
        """Return every ``serving_geo.raster_layer_v`` row where
        ``published=true``. Used by the tile-source resolver to emit a
        titiler URL per row. Compute paths read via ``get_by_source``
        instead — they must not be gated on ``published=true``.
        """
        ...

    def set_published(self, *, layer_slug: str, published: bool) -> None:
        """Flip ``published`` for the row with the given ``layer_slug``.
        Backs the ``reimagina raster publish`` / ``unpublish`` CLI.
        """
        ...


class AggregationRecipeRepository(Protocol):
    """Declarative recipes binding an input source + method + target grain.

    ``curated_geo.aggregation_recipe`` is the canonical write target
    (r020); read-side methods go through
    ``serving_geo.aggregation_recipe_v`` so workers honour the
    "API/workers read only serving_geo" rule.

    One recipe = "produce observations at ``target_feature_type_slug``
    under ``attribute_key_prefix`` via ``method`` from this input". The
    input is either a raster (``input_kind='raster'``) or the set of
    current observations at a finer grain (``input_kind='observation'``);
    the XOR is enforced by a DB CHECK.
    """

    def register_recipe(
        self,
        *,
        input_kind: str,
        target_feature_type_slug: str,
        attribute_key_prefix: str,
        method: str,
        input_raster_layer_id: str | None = None,
        input_attribute_key_prefix: str | None = None,
        input_feature_type_slug: str | None = None,
        weight_basis: str | None = None,
        params: dict[str, Any] | None = None,
        coverage_threshold: float = 0.5,
        aoi_id: str | None = None,
        enabled: bool = True,
    ) -> str:
        """Insert a new recipe row, return the new id. The DB enforces the
        XOR between raster / observation input shapes.
        """
        ...

    def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        """Return a single recipe (from ``serving_geo.aggregation_recipe_v``)
        or None. Dispatcher looks up by id and branches on ``input_kind``.
        """
        ...

    def list_recipes(
        self,
        *,
        input_raster_layer_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List recipes from ``serving_geo.aggregation_recipe_v``. Filter
        by raster layer for the common "run all recipes for layer X"
        command; ``enabled_only`` hides disabled rows so the dispatcher
        never runs them by accident.
        """
        ...

    def set_enabled(self, *, recipe_id: str, enabled: bool) -> None:
        """Flip ``enabled`` on one recipe. Parallel to
        ``RasterLayerRepository.set_published``.
        """
        ...

    def delete_recipe(self, recipe_id: str) -> None:
        """Hard-delete a recipe. Observations it produced stay put —
        operators retire them via ``retire_current_by_prefix`` when
        they decide the prefix is no longer wanted.
        """
        ...


class AOIRepository(Protocol):
    def create_aoi(
        self,
        *,
        project_id: str,
        name: str,
        geometry_wkt: str,
        description: str | None = None,
    ) -> str: ...

    def compute_overlaps(self, aoi_id: str) -> int: ...

    def list_aois(self) -> list[dict[str, Any]]:
        """Return every AOI as ``{aoi_id, project_id, name, description}``."""
        ...

    def get_aoi(self, aoi_id: str) -> dict[str, Any] | None:
        """Return one AOI or ``None``; shape matches ``list_aois`` rows."""
        ...

    def upsert_project_by_name(self, name: str, description: str | None = None) -> str:
        """Return the ``app_core.project`` id for the row with the given name,
        creating it first if needed. Intended for dev/bootstrap flows that
        want one project row to hang data off without coordinating ids.
        """
        ...


class OverlapRepository(Protocol):
    def compute_feature_overlaps(
        self,
        source_type_id: str,
        target_type_id: str,
        *,
        aoi_id: str | None = None,
    ) -> int:
        """Compute and store overlap rows for all feature pairs of the given types.

        Measures are geodesic (m² for area, m for length, dimensionless for presence).
        When aoi_id is provided, only source features intersecting that AOI are
        considered — use this at city scale to avoid unbounded cross joins.
        Returns the number of overlap rows written (upserted).
        """
        ...

    def rollup_child_rows(
        self,
        *,
        indicator_slug: str,
        source_feature_type: str,
        target_feature_type: str,
        scenario_id: str | None = None,
    ) -> list[tuple[str, float | None, float, str]]:
        """Return ``(target_id, value_numeric, overlap_measure, quality_flag)`` per
        (source, target) overlap pair, restricted to the given feature types and
        the current indicator row for the given scenario scope.

        Weight is ``overlap_measure`` (absolute intersection, m² for polygon
        rollups) so ``weighted_mean`` is area-weighted. ``overlap_ratio`` is
        wrong here: fully-nested children collapse to ~1.0 and degrade the
        weighted mean to an arithmetic mean. Quality defaults to ``'missing'``
        when the LEFT join misses an indicator row. Callers group by target
        themselves.
        """
        ...

    def rollup_observation_child_rows(
        self,
        *,
        input_attribute_key_prefix: str,
        source_feature_type: str,
        target_feature_type: str,
        aoi_id: str | None = None,
    ) -> list[tuple[str, str, float, float]]:
        """Return ``(target_id, source_attribute_key, value, overlap_measure)``
        per (source observation, target feature) overlap pair.

        Reads current observations at ``source_feature_type`` whose
        ``attribute_key`` starts with ``:prefix`` or equals it exactly
        (same separator-safe matching as ``retire_current_by_prefix``),
        joins to ``feature_overlap``, restricts to target features of
        ``target_feature_type``. Weight is ``overlap_measure`` for the
        same reason ``rollup_child_rows`` picks it. Callers group by
        ``(target_id, source_attribute_key)`` and aggregate per group
        so a prefix that spans multiple stat suffixes (e.g.
        ``pop_sum`` + ``pop_count``) rolls up each suffix independently.

        Inner join on observation (unlike the LEFT join on indicator_result
        for the indicator rollup): observations that don't exist aren't
        "missing", they're absent — workers don't emit placeholder rows
        for them.
        """
        ...


class OverlapTypePairRepository(Protocol):
    """Declared (source_type, target_type) pairs that the overlap worker computes.

    Kept separate from OverlapRepository so that declaration (seed-time) and
    computation (run-time) don't share a connection-facing type.
    """

    def upsert_pair(self, source_type_id: str, target_type_id: str) -> str: ...

    def list_pairs(self) -> list[dict[str, Any]]: ...


class ServingReader(Protocol):
    def get_features(
        self,
        *,
        type_slug: str | None = None,
        role: str | None = None,
        aoi_id: str | None = None,
        limit: int = 500,
        after_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return {"features": list[dict], "next_cursor": str | None}.

        Results are ordered by (created_at, id) and bounded by `limit`.
        Pass `after_cursor` from a previous response to fetch the next page.
        """
        ...

    def count_features(self, *, type_slug: str | None = None) -> int:
        """Return the total number of serving-layer features (optionally filtered by type)."""
        ...

    def count_features_within_polygon(
        self,
        *,
        aoi_id: str,
        polygon_geojson: str,
        type_slugs: list[str],
    ) -> dict[str, int]:
        """Count features intersecting a GeoJSON polygon, grouped by type_slug.

        Polygon must be a valid GeoJSON Polygon string in EPSG:4326.
        Returns ``{type_slug: count}`` for every requested type that has at
        least one intersecting feature; missing types resolve to 0 at the
        caller.
        """
        ...


class ExploreReader(Protocol):
    def list_layers(self) -> list[dict[str, Any]]: ...

    def list_sources(self) -> list[dict[str, Any]]: ...

    def list_feature_stats(self) -> list[dict[str, Any]]: ...

    def list_serving_stats(self) -> list[dict[str, Any]]: ...

    def list_executor_health(self) -> list[dict[str, Any]]: ...


class JobRunRepository(Protocol):
    """Provenance record of a compute run (jobs_runtime.job_run).

    A run is created via start_job (typically straight to 'running' by the
    job_run lifecycle helper) and terminated via complete_job with 'succeeded'
    or 'failed'. Every indicator_result row must cite a job_run by id.

    Ops-plane scheduling and claiming belong to the ops-plane, not here —
    there is no claim_job.
    """

    def start_job(
        self,
        *,
        model_family: str,
        code_version: str,
        scenario_id: str | None = None,
        status: str = "queued",
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str: ...

    def complete_job(
        self,
        job_run_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None: ...

    def get_job(self, job_run_id: str) -> dict[str, Any] | None: ...

    def cancel_run_backends(self, run_id: str) -> int:
        """Cancel the in-flight query of every live backend for ``run_id``.

        The ops-plane's own cancel only stops *scheduling*; interrupting a
        statement already executing needs the database. Returns the number of
        backends signalled.
        """
        ...


class IndicatorCatalogRepository(Protocol):
    """CRUD for indicator_catalog + indicator_dependency rows."""

    def upsert_indicator(
        self,
        *,
        slug: str,
        name: str,
        computation_kind: str,
        value_type: str,
        aggregation_method: str,
        applies_to: list[str],
        description: str | None = None,
        model_family: str | None = None,
        unit: str | None = None,
        valid_min: float | None = None,
        valid_max: float | None = None,
    ) -> str: ...

    def get_indicator(self, slug: str) -> dict[str, Any] | None: ...

    def list_indicators(self) -> list[dict[str, Any]]: ...

    def set_dependencies(
        self,
        *,
        indicator_slug: str,
        depends_on: list[tuple[str, float | None]],
    ) -> None: ...


class IndicatorResultRepository(Protocol):
    """Write + read path for indicator_result.

    Writers go through write_result, which enforces exactly-one-value-column,
    valid_min/valid_max bounds, applies_to membership, and missing_reason
    rules — matching the Postgres before-write trigger.

    The `current`-status uniqueness guarantee is preserved: write_result
    marks any existing `current` row for the same
    (feature_id, scenario_id, indicator_id) stale before inserting the new
    one, so reads through serving_geo.current_indicator_v always see at most
    one row per triple.
    """

    def write_result(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        job_run_id: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
        value_json: Any = None,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
        confidence: float | None = None,
        missing_reason: str | None = None,
        model_version: str | None = None,
    ) -> str: ...

    def bulk_write_numeric_results(
        self,
        rows: list[tuple[str, float]],
        *,
        indicator_slug: str,
        job_run_id: str,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
    ) -> int:
        """Fast path for many per-feature numeric results from one job_run.

        Each tuple is (feature_id, value_numeric). Returns number of rows
        written. Supersedes existing `current` rows for each feature.
        """
        ...

    def get_current(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_current_for_indicator(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_current_with_geometry(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Like list_current_for_indicator but also returns geometry_wkb.

        Result dicts carry `feature_id`, `type_slug`, `geometry_wkb` (bytes,
        EPSG:4326 WKB), `value_numeric`, `quality_flag`, and `computed_at`.
        Intended to back the API's GeoJSON / GeoParquet download endpoints
        without a second round trip.
        """
        ...

    def value_stats(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        aoi_id: str | None = None,
    ) -> dict[str, Any]:
        """Summary statistics for a numeric indicator's current values.

        Returns a dict with `count`, `min`, `p05`, `p50`, `p95`, `max`
        (all `None` when there are no rows). Intended to drive the
        frontend's choropleth colour ramp from the real distribution
        rather than the catalog's safety bounds.

        ``aoi_id`` scopes the stats to that AOI's features only via
        ``curated_geo.aoi_feature``. Multi-site UIs need this so
        Mexicali's colour ramp doesn't get pulled by CDMX's distribution.
        """
        ...


class AOISummaryRepository(Protocol):
    def upsert_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        value: float | None,
        scenario_id: str | None = None,
        aggregation_note: str | None = None,
        status: str = "current",
    ) -> str: ...

    def get_summary(
        self,
        *,
        aoi_id: str,
        indicator_id: str,
        scenario_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_summaries(
        self,
        *,
        aoi_id: str,
        scenario_id: str | None = None,
        only_current: bool = True,
    ) -> list[dict[str, Any]]: ...

    def fetch_aoi_children(
        self,
        *,
        aoi_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> list[tuple[float | None, float, str]]:
        """Return ``(value, weight, quality_flag)`` for each feature in the AOI.

        Joins ``aoi_feature`` to ``indicator_result`` on the given slug +
        scenario scope. ``weight`` is ``aoi_feature.overlap_measure``
        (absolute area) — ``overlap_ratio`` would collapse fully-nested
        children to 1.0 and degrade weighted aggregates to an arithmetic
        mean. Features with no current indicator row appear with
        ``value=None`` and ``quality='missing'`` so the worker's
        missing-weight policy can account for them.
        """
        ...


class TileSnapshotRepository(Protocol):
    def register_snapshot(
        self,
        *,
        layer_id: str,
        artifact_path: str,
        artifact_uri: str | None,
        snapshot_version: str,
        source_relation: str,
        source_lineage_ref: dict[str, Any] | None = None,
        status: str = "ready",
    ) -> str: ...

    def get_latest_snapshot(self, *, layer_id: str) -> dict[str, Any] | None: ...

    def list_snapshots(
        self, *, layer_id: str | None = None
    ) -> list[dict[str, Any]]: ...


class RunPlanRepository(Protocol):
    """The resolved feature set for a run (jobs_runtime.run_plan).

    Write-once. The submitter resolves the plan and stores it; each unit reads
    back only its own slice. Keeping it here rather than in the workflow input is
    what stops the payload scaling with the size of the city.
    """

    def write(self, *, plan_id: str, units: dict[str, list[str]]) -> None: ...

    def features_for(self, *, plan_id: str, indicator_slug: str) -> list[str]: ...

    def delete(self, *, plan_id: str) -> int: ...

    def sweep(self, *, older_than_hours: int) -> int: ...


class StorageBackend(Protocol):
    name: str
    connection: Any

    def verify_required_relations(self, relations: list[str]) -> dict[str, bool]: ...

    def catalog_repo(self) -> SourceCatalogRepository: ...

    def feature_repo(self) -> FeatureRepository: ...

    def observation_repo(self) -> ObservationRepository: ...

    def file_asset_repo(self) -> FileAssetRepository: ...

    def raster_layer_repo(self) -> RasterLayerRepository: ...

    def aggregation_recipe_repo(self) -> AggregationRecipeRepository: ...

    def aoi_repo(self) -> AOIRepository: ...

    def overlap_repo(self) -> OverlapRepository: ...

    def overlap_type_pair_repo(self) -> OverlapTypePairRepository: ...

    def serving_reader(self) -> ServingReader: ...

    def explore_reader(self) -> ExploreReader: ...

    def tile_repo(self) -> TileSnapshotRepository: ...

    def aoi_summary_repo(self) -> AOISummaryRepository: ...

    def job_run_repo(self) -> JobRunRepository: ...

    def indicator_catalog_repo(self) -> IndicatorCatalogRepository: ...

    def indicator_result_repo(self) -> IndicatorResultRepository: ...

    def run_plan_repo(self) -> RunPlanRepository: ...
