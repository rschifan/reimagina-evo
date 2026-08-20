"""Canonical geospatial entities and their observed attributes.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import geopandas as gpd


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

    def retire_current_by_prefix(
        self,
        *,
        prefix: str,
        feature_type_id: str,
        aoi_id: str | None = None,
    ) -> int:
        """Retire every current observation whose ``attribute_key`` either
        equals ``:prefix`` exactly or starts with ``:prefix_`` (literal
        separator, NOT a SQL LIKE wildcard), **within the scope the caller
        recomputed**.

        Implementations use ``starts_with(attribute_key, :prefix || '_')``
        so ``prefix='ndvi'`` does not accidentally retire ``ndvi2_mean``.

        ``feature_type_id`` is required and restricts retirement to
        observations on features of that grain. It is not optional because
        a producer only ever writes one grain: ``aggregation_recipe``'s
        uniqueness key is ``(target_feature_type_slug,
        attribute_key_prefix)`` (r020), so two recipes may legitimately
        publish the same prefix at different grains, and an unscoped
        retire lets either run silently wipe the other's values before
        re-inserting only its own.

        ``aoi_id`` restricts it further to features in that AOI, and must
        be passed whenever the run itself was AOI-scoped. Without it an
        AOI-scoped run retires the prefix for every feature of the grain
        in the database and then re-inserts only the in-AOI ones, so
        everything outside the AOI loses its value permanently.

        Both filters partition on ``feature_id``, which is why they are
        safe: ``obs_one_current`` is unique on
        ``(feature_id, attribute_key) WHERE is_current`` (r003), so
        narrowing by grain or AOI can never leave two current rows for one
        pair. Retirement stays **cross-source** for exactly that reason —
        the invariant ignores ``source_id``, so retiring one source's rows
        alone would leave it broken. Callers wanting per-source ownership
        enforce it up front via ``has_active_prefix``.

        Raises ``ValueError`` on an empty prefix or an empty
        ``feature_type_id``, both to prevent catch-all retirement. Returns
        the number of rows transitioned to ``is_current = false``.
        """
        ...

    def has_active_prefix(
        self,
        *,
        prefix: str,
        excluding_source_id: str,
        feature_type_id: str,
    ) -> bool:
        """Return True iff any ``curated_geo.observation`` row exists with
        ``is_current = true``, ``source_id != :excluding_source_id``, an
        ``attribute_key`` equal to ``:prefix`` or matching
        ``:prefix_<suffix>`` under the same separator-safe pattern as
        ``retire_current_by_prefix``, **on a feature of
        ``:feature_type_id``**.

        No "active source" concept — what matters for SCD safety is
        whether some other source's current observation already owns
        the prefix, because the writer retires/upserts on
        ``(feature_id, attribute_key)`` regardless of source. Published
        status is irrelevant. Backs the CLI prefix-collision check.

        ``feature_type_id`` is required and mirrors
        ``retire_current_by_prefix``'s grain scope, because a guard that
        does not match what the action can actually reach is either
        wrong or useless. Unscoped, this refused two recipes owning one
        prefix at two different grains — supported configuration under
        ``aggregation_recipe_prefix_uk`` (r020), and safe now that
        retirement is grain-scoped.

        Deliberately **not** AOI-scoped, unlike retirement. A guard has
        to cover the widest thing a run could retire, and an AOI is only
        a narrowing the caller opts into: a whole-grain run retires the
        whole grain, so a collision outside any given AOI is still a
        collision.
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
        membership_key: dict[str, str] | None = None,
    ) -> str: ...

    def compute_overlaps(self, aoi_id: str) -> int:
        """(Re)populate ``aoi_feature`` for one AOI. Returns rows inserted.

        Membership is decided per feature, not per AOI (r040): a feature
        carrying every key in the AOI's ``membership_key`` must match those
        values exactly, and one carrying none of them falls back to spatial
        overlap above ``MEMBERSHIP_MIN_OVERLAP_RATIO``. Both paths exist
        because INEGI features have authoritative identifiers and Overture
        buildings have none.
        """
        ...

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
        ``target_feature_type``. Weight is ``overlap_measure`` — the
        absolute area inside the target, not the ratio, so parents of
        different sizes contribute proportionally. Callers group by
        ``(target_id, source_attribute_key)`` and aggregate per group
        so a prefix that spans multiple stat suffixes (e.g.
        ``pop_sum`` + ``pop_count``) rolls up each suffix independently.

        Inner join on observation: observations that don't exist aren't
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
