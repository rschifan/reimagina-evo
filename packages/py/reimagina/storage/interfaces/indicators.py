"""The indicator catalog and its computed results.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from typing import Any, Protocol


class IndicatorCatalogRepository(Protocol):
    """CRUD for indicator_catalog + indicator_dependency rows."""

    def upsert_indicator(
        self,
        *,
        slug: str,
        name: str,
        computation_kind: str,
        value_type: str,
        aggregation_method: str,
        applies_to: list[str],
        description: str | None = None,
        model_family: str | None = None,
        unit: str | None = None,
        valid_min: float | None = None,
        valid_max: float | None = None,
    ) -> str: ...

    def get_indicator(self, slug: str) -> dict[str, Any] | None: ...

    def list_indicators(
        self, *, with_values_for_aoi: str | None = None
    ) -> list[dict[str, Any]]: ...

    def set_dependencies(
        self,
        *,
        indicator_slug: str,
        depends_on: list[tuple[str, float | None]],
    ) -> None: ...

    def list_dependencies(self) -> list[dict[str, Any]]: ...


class IndicatorResultRepository(Protocol):
    """Write + read path for indicator_result.

    Writers go through write_result, which enforces exactly-one-value-column,
    valid_min/valid_max bounds, applies_to membership, and missing_reason
    rules — matching the Postgres before-write trigger.

    The `current`-status uniqueness guarantee is preserved: write_result
    marks any existing `current` row for the same
    (feature_id, scenario_id, indicator_id) stale before inserting the new
    one, so reads through serving_geo.current_indicator_v always see at most
    one row per triple.
    """

    def write_result(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        job_run_id: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
        value_json: Any = None,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
        confidence: float | None = None,
        missing_reason: str | None = None,
        model_version: str | None = None,
    ) -> str: ...

    def bulk_write_numeric_results(
        self,
        rows: list[tuple[str, float]],
        *,
        indicator_slug: str,
        job_run_id: str,
        scenario_id: str | None = None,
        quality_flag: str = "measured",
    ) -> int:
        """Fast path for many per-feature numeric results from one job_run.

        Each tuple is (feature_id, value_numeric). Returns number of rows
        written. Supersedes existing `current` rows for each feature.
        """
        ...

    def get_current(
        self,
        *,
        feature_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_current_for_indicator(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_current_with_geometry(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Like list_current_for_indicator but also returns geometry_wkb.

        Result dicts carry `feature_id`, `type_slug`, `geometry_wkb` (bytes,
        EPSG:4326 WKB), `value_numeric`, `quality_flag`, and `computed_at`.
        Intended to back the API's GeoJSON / GeoParquet download endpoints
        without a second round trip.
        """
        ...

    def value_stats(
        self,
        *,
        indicator_slug: str,
        scenario_id: str | None = None,
        feature_type: str | None = None,
        aoi_id: str | None = None,
        bins: int = 0,
    ) -> dict[str, Any]:
        """Summary statistics for a numeric indicator's current values.

        Returns a dict with `count`, `min`, `p05`, `p50`, `p95`, `max`
        (all `None` when there are no rows). Intended to drive the
        frontend's choropleth colour ramp from the real distribution
        rather than the catalog's safety bounds.

        ``aoi_id`` scopes the stats to that AOI's features only via
        ``curated_geo.aoi_feature``. Multi-site UIs need this so
        Mexicali's colour ramp doesn't get pulled by CDMX's distribution.

        ``bins`` greater than zero adds a ``histogram`` key:
        ``{bins, lo, hi, width, counts}``, equal-width over the **full**
        observed range, computed from the same population as the
        percentiles above. ``None`` when there is nothing to bin.

        The histogram is server-side rather than derived from what the map
        has rendered, and that is a constraint rather than a preference:
        ``selection-metrics`` in the web app abandoned
        ``queryRenderedFeatures`` precisely because it "silently undercounted
        whenever the renderer hadn't loaded the relevant tiles". A
        distribution that changes shape while tiles load is worse than none,
        because a reader cannot tell which shape was the real one.
        """
        ...


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

    def fetch_aoi_children(
        self,
        *,
        aoi_id: str,
        indicator_slug: str,
        scenario_id: str | None = None,
    ) -> list[tuple[float | None, float, str]]:
        """Return ``(value, weight, quality_flag)`` for each feature in the AOI.

        Joins ``aoi_feature`` to ``indicator_result`` on the given slug +
        scenario scope. ``weight`` is ``aoi_feature.overlap_measure``
        (absolute area) — ``overlap_ratio`` would collapse fully-nested
        children to 1.0 and degrade weighted aggregates to an arithmetic
        mean. Features with no current indicator row appear with
        ``value=None`` and ``quality='missing'`` so the worker's
        missing-weight policy can account for them.
        """
        ...
