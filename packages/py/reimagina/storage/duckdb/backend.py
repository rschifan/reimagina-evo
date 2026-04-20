"""DuckDB + spatial storage backend wiring."""
from __future__ import annotations

from .migrations import run_duckdb_migrations
from .repositories import (
    DuckDBAOIRepository,
    DuckDBAOISummaryRepository,
    DuckDBExploreReader,
    DuckDBFeatureRepository,
    DuckDBFileAssetRepository,
    DuckDBIndicatorCatalogRepository,
    DuckDBIndicatorResultRepository,
    DuckDBJobRunRepository,
    DuckDBObservationRepository,
    DuckDBOverlapRepository,
    DuckDBOverlapTypePairRepository,
    DuckDBServingReader,
    DuckDBSourceCatalogRepository,
    DuckDBTileSnapshotRepository,
)


class DuckDBStorageBackend:
    name = "duckdb"

    def __init__(self, connection):
        self.connection = connection

    def run_migrations(self) -> None:
        run_duckdb_migrations(self.connection)

    def verify_required_relations(self, relations: list[str]) -> dict[str, bool]:
        checks: dict[str, bool] = {}
        for relation in relations:
            schema_name, object_name = relation.split(".", 1)
            row = self.connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT table_schema AS schema_name, table_name AS object_name FROM information_schema.tables
                    UNION ALL
                    SELECT table_schema AS schema_name, table_name AS object_name FROM information_schema.views
                ) rels
                WHERE schema_name = ? AND object_name = ?
                """,
                [schema_name, object_name],
            ).fetchone()
            checks[relation] = bool(row and row[0] > 0)
        return checks

    def catalog_repo(self) -> DuckDBSourceCatalogRepository:
        return DuckDBSourceCatalogRepository(self.connection)

    def feature_repo(self) -> DuckDBFeatureRepository:
        return DuckDBFeatureRepository(self.connection)

    def observation_repo(self) -> DuckDBObservationRepository:
        return DuckDBObservationRepository(self.connection)

    def file_asset_repo(self) -> DuckDBFileAssetRepository:
        return DuckDBFileAssetRepository(self.connection)

    def aoi_repo(self) -> DuckDBAOIRepository:
        return DuckDBAOIRepository(self.connection)

    def overlap_repo(self) -> DuckDBOverlapRepository:
        return DuckDBOverlapRepository(self.connection)

    def overlap_type_pair_repo(self) -> DuckDBOverlapTypePairRepository:
        return DuckDBOverlapTypePairRepository(self.connection)

    def serving_reader(self) -> DuckDBServingReader:
        return DuckDBServingReader(self.connection)

    def explore_reader(self) -> DuckDBExploreReader:
        return DuckDBExploreReader(self.connection)

    def tile_repo(self) -> DuckDBTileSnapshotRepository:
        return DuckDBTileSnapshotRepository(self.connection)

    def aoi_summary_repo(self) -> DuckDBAOISummaryRepository:
        return DuckDBAOISummaryRepository(self.connection)

    def job_run_repo(self) -> DuckDBJobRunRepository:
        return DuckDBJobRunRepository(self.connection)

    def indicator_catalog_repo(self) -> DuckDBIndicatorCatalogRepository:
        return DuckDBIndicatorCatalogRepository(self.connection)

    def indicator_result_repo(self) -> DuckDBIndicatorResultRepository:
        return DuckDBIndicatorResultRepository(self.connection)
