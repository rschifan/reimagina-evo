"""Backend capability flags used to keep portability explicit."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageCapabilities:
    backend_name: str
    supports_triggers: bool
    supports_materialized_views: bool
    supports_spatial_index: bool
    supports_partial_indexes: bool
    supports_schema_namespaces: bool = True
