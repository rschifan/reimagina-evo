"""Read-side surfaces: what the API and tiles consume.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from typing import Any, Protocol


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

    def count_features(self, *, type_slug: str | None = None) -> int:
        """Return the total number of serving-layer features (optionally filtered by type)."""
        ...

    def count_features_within_polygon(
        self,
        *,
        aoi_id: str,
        polygon_geojson: str,
        type_slugs: list[str],
    ) -> dict[str, int]:
        """Count features intersecting a GeoJSON polygon, grouped by type_slug.

        Polygon must be a valid GeoJSON Polygon string in EPSG:4326.
        Returns ``{type_slug: count}`` for every requested type that has at
        least one intersecting feature; missing types resolve to 0 at the
        caller.
        """
        ...


class ExploreReader(Protocol):
    def list_layers(self) -> list[dict[str, Any]]: ...

    def list_sources(self) -> list[dict[str, Any]]: ...

    def list_feature_stats(self) -> list[dict[str, Any]]: ...

    def list_serving_stats(self) -> list[dict[str, Any]]: ...

    def list_executor_health(self) -> list[dict[str, Any]]: ...


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

    def list_snapshots(
        self, *, layer_id: str | None = None
    ) -> list[dict[str, Any]]: ...
