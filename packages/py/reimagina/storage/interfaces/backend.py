"""The accessor every consumer holds.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from typing import Any, Protocol


from reimagina.storage.interfaces.sources import (
    SourceCatalogRepository,
    FileAssetRepository,
)
from reimagina.storage.interfaces.features import (
    FeatureRepository,
    ObservationRepository,
    AOIRepository,
    OverlapRepository,
    OverlapTypePairRepository,
)
from reimagina.storage.interfaces.raster import (
    RasterLayerRepository,
    AggregationRecipeRepository,
)
from reimagina.storage.interfaces.indicators import (
    IndicatorCatalogRepository,
    IndicatorResultRepository,
    AOISummaryRepository,
)
from reimagina.storage.interfaces.jobs import JobRunRepository, RunPlanRepository
from reimagina.storage.interfaces.serving import (
    ServingReader,
    ExploreReader,
    TileSnapshotRepository,
)


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
