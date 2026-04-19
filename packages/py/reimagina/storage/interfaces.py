"""Operation-shaped storage interfaces for the data layer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import geopandas as gpd

from .capabilities import StorageCapabilities


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
        materialising it fully in Python.  Both the Postgres and DuckDB backends
        provide native implementations; the loader falls back to the GeoDataFrame
        path if a future backend omits this method.
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


class ExploreReader(Protocol):
    def list_layers(self) -> list[dict[str, Any]]: ...

    def list_sources(self) -> list[dict[str, Any]]: ...

    def list_feature_stats(self) -> list[dict[str, Any]]: ...

    def list_serving_stats(self) -> list[dict[str, Any]]: ...


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

    def list_snapshots(self, *, layer_id: str | None = None) -> list[dict[str, Any]]: ...


class StorageBackend(Protocol):
    name: str
    connection: Any

    def capabilities(self) -> StorageCapabilities: ...

    def run_migrations(self) -> None: ...

    def verify_required_relations(self, relations: list[str]) -> dict[str, bool]: ...

    def catalog_repo(self) -> SourceCatalogRepository: ...

    def feature_repo(self) -> FeatureRepository: ...

    def observation_repo(self) -> ObservationRepository: ...

    def file_asset_repo(self) -> FileAssetRepository: ...

    def aoi_repo(self) -> AOIRepository: ...

    def overlap_repo(self) -> OverlapRepository: ...

    def overlap_type_pair_repo(self) -> OverlapTypePairRepository: ...

    def serving_reader(self) -> ServingReader: ...

    def explore_reader(self) -> ExploreReader: ...

    def tile_repo(self) -> TileSnapshotRepository: ...

    def aoi_summary_repo(self) -> AOISummaryRepository: ...


@dataclass(frozen=True)
class BackendConnectionSpec:
    dsn: str
    path: Path | None = None
