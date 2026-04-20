"""PostgreSQL/PostGIS storage backend wiring."""
from __future__ import annotations

from .repositories import (
    PostgresAOIRepository,
    PostgresAOISummaryRepository,
    PostgresExploreReader,
    PostgresFeatureRepository,
    PostgresFileAssetRepository,
    PostgresIndicatorCatalogRepository,
    PostgresIndicatorResultRepository,
    PostgresJobRunRepository,
    PostgresObservationRepository,
    PostgresOverlapRepository,
    PostgresOverlapTypePairRepository,
    PostgresServingReader,
    PostgresSourceCatalogRepository,
    PostgresTileSnapshotRepository,
)


class PostgresStorageBackend:
    name = "postgres"

    def __init__(self, connection):
        self.connection = connection

    def run_migrations(self) -> None:
        raise NotImplementedError("Use the postgres migration runner with a DSN")

    def verify_required_relations(self, relations: list[str]) -> dict[str, bool]:
        checks: dict[str, bool] = {}
        with self.connection.cursor() as cur:
            for relation in relations:
                cur.execute("SELECT to_regclass(%s)", (relation,))
                checks[relation] = cur.fetchone()[0] is not None
        return checks

    def catalog_repo(self) -> PostgresSourceCatalogRepository:
        return PostgresSourceCatalogRepository(self.connection)

    def feature_repo(self) -> PostgresFeatureRepository:
        return PostgresFeatureRepository(self.connection)

    def observation_repo(self) -> PostgresObservationRepository:
        return PostgresObservationRepository(self.connection)

    def file_asset_repo(self) -> PostgresFileAssetRepository:
        return PostgresFileAssetRepository(self.connection)

    def aoi_repo(self) -> PostgresAOIRepository:
        return PostgresAOIRepository(self.connection)

    def overlap_repo(self) -> PostgresOverlapRepository:
        return PostgresOverlapRepository(self.connection)

    def overlap_type_pair_repo(self) -> PostgresOverlapTypePairRepository:
        return PostgresOverlapTypePairRepository(self.connection)

    def serving_reader(self) -> PostgresServingReader:
        return PostgresServingReader(self.connection)

    def explore_reader(self) -> PostgresExploreReader:
        return PostgresExploreReader(self.connection)

    def tile_repo(self) -> PostgresTileSnapshotRepository:
        return PostgresTileSnapshotRepository(self.connection)

    def aoi_summary_repo(self) -> PostgresAOISummaryRepository:
        return PostgresAOISummaryRepository(self.connection)

    def job_run_repo(self) -> PostgresJobRunRepository:
        return PostgresJobRunRepository(self.connection)

    def indicator_catalog_repo(self) -> PostgresIndicatorCatalogRepository:
        return PostgresIndicatorCatalogRepository(self.connection)

    def indicator_result_repo(self) -> PostgresIndicatorResultRepository:
        return PostgresIndicatorResultRepository(self.connection)
