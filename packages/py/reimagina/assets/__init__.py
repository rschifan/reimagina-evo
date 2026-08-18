"""The asset graph: one dependency graph over everything the platform produces.

See ``infra/db/migrations/versions/r041_asset_graph.py`` for the schema and the
reasoning. In short: every named artifact is an asset, a source asset's version
is recorded by whatever changed it, a derived asset's version is a hash of its
upstream closure, and "stale" is ``materialized <> expected`` — computed on read
rather than marked by a trigger somebody has to remember to fire.
"""

from __future__ import annotations

from reimagina.assets.keys import partition_key
from reimagina.assets.registry import AssetSpec, SHIPPED_ASSETS, seed_assets
from reimagina.assets.version import expected_versions

__all__ = [
    "AssetSpec",
    "SHIPPED_ASSETS",
    "expected_versions",
    "partition_key",
    "seed_assets",
]
