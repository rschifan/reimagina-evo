"""Read-side Protocol for the asset graph.

`reimagina.assets` writes the graph through raw cursors; this is the first
reader that is not a view. Kept to reads: seeding and publishing already have
their own entry points, and mixing them here would give the catalog page a
write path it has no business holding.
"""

from __future__ import annotations

from typing import Any, Protocol


class AssetGraphRepository(Protocol):
    def get_asset(self, asset_key: str) -> dict[str, Any] | None: ...

    def lineage(
        self, *, asset_key: str, direction: str, max_depth: int | None = None
    ) -> list[dict[str, Any]]: ...

    def freshness(
        self, *, asset_keys: list[str] | None = None
    ) -> list[dict[str, Any]]: ...
