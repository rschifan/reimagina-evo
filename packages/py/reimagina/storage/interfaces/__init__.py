"""Operation-shaped storage interfaces for the data layer.

One Protocol per repository, 89 methods over a single Postgres backend.
That is not speculation about a second backend — it is the only place 29 of
those methods are documented at all, and since mypy runs in CI it is an
enforced contract rather than prose. Split across modules by domain purely
so no one file is 800 lines; import from here.
"""

from __future__ import annotations

from reimagina.storage.interfaces.sources import (
    SourceCatalogRepository as SourceCatalogRepository,
    FileAssetRepository as FileAssetRepository,
)
from reimagina.storage.interfaces.assets import (
    AssetGraphRepository as AssetGraphRepository,
)
from reimagina.storage.interfaces.features import (
    FeatureRepository as FeatureRepository,
    ObservationRepository as ObservationRepository,
    AOIRepository as AOIRepository,
    OverlapRepository as OverlapRepository,
    OverlapTypePairRepository as OverlapTypePairRepository,
)
from reimagina.storage.interfaces.raster import (
    RasterLayerRepository as RasterLayerRepository,
    AggregationRecipeRepository as AggregationRecipeRepository,
)
from reimagina.storage.interfaces.indicators import (
    IndicatorCatalogRepository as IndicatorCatalogRepository,
    IndicatorResultRepository as IndicatorResultRepository,
    AOISummaryRepository as AOISummaryRepository,
)
from reimagina.storage.interfaces.jobs import (
    JobRunRepository as JobRunRepository,
    OpsRunReader as OpsRunReader,
    RunPlanRepository as RunPlanRepository,
)
from reimagina.storage.interfaces.serving import (
    ServingReader as ServingReader,
    ExploreReader as ExploreReader,
    TileSnapshotRepository as TileSnapshotRepository,
)
from reimagina.storage.interfaces.backend import StorageBackend as StorageBackend

__all__ = [
    "AssetGraphRepository",
    "AOIRepository",
    "AOISummaryRepository",
    "AggregationRecipeRepository",
    "ExploreReader",
    "FeatureRepository",
    "FileAssetRepository",
    "IndicatorCatalogRepository",
    "IndicatorResultRepository",
    "JobRunRepository",
    "OpsRunReader",
    "ObservationRepository",
    "OverlapRepository",
    "OverlapTypePairRepository",
    "RasterLayerRepository",
    "RunPlanRepository",
    "ServingReader",
    "SourceCatalogRepository",
    "StorageBackend",
    "TileSnapshotRepository",
]
