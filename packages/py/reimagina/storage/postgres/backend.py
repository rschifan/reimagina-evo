"""PostgreSQL/PostGIS storage backend wiring."""
from __future__ import annotations

from .repositories import (
    PostgresAOIRepository,
    PostgresExploreReader,
    PostgresFeatureRepository,
    PostgresFileAssetRepository,
    PostgresObservationRepository,
    PostgresOverlapRepository,
    PostgresServingReader,
    PostgresSourceCatalogRepository,
)
from ..capabilities import StorageCapabilities


class PostgresStorageBackend:
    name = "postgres"

    def __init__(self, connection):
        self.connection = connection

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            backend_name="postgres",
            supports_triggers=True,
            supports_materialized_views=True,
            supports_spatial_index=True,
            supports_partial_indexes=True,
        )

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

    def serving_reader(self) -> PostgresServingReader:
        return PostgresServingReader(self.connection)

    def explore_reader(self) -> PostgresExploreReader:
        return PostgresExploreReader(self.connection)
