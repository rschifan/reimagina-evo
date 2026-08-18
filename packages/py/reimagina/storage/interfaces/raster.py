"""Raster layers and the recipes that reduce them onto features.

Part of ``reimagina.storage.interfaces``; import from the package, not this
module. Split by domain because 18 Protocols in one file was a scroll, not
because the count is a problem — see the package docstring.
"""

from __future__ import annotations

from typing import Any, Protocol


class RasterLayerRepository(Protocol):
    """Registry of raster tile layers — one row per published raster.

    ``curated_geo.raster_layer`` is the canonical write target (r019);
    read-side methods go through ``serving_geo.raster_layer_v`` so
    workers honour the "API/workers read only serving_geo" rule.

    Three real consumers earn the Protocol's place: the R2 ingest
    pipeline (``register_layer``), the R3 zonal-stats worker
    (``get_by_source`` to read ``aggregation_kind``), and the R4 tile-
    source resolver (``list_published_layers``).
    """

    def register_layer(
        self,
        *,
        source_id: str,
        asset_id: str,
        layer_slug: str,
        logical_name: str | None = None,
        description: str | None = None,
        colormap_name: str | None = None,
        rescale: str | None = None,
        minzoom: int = 5,
        maxzoom: int = 14,
        published: bool = True,
        aggregation_kind: str = "intensive",
    ) -> str:
        """Insert a new ``curated_geo.raster_layer`` row. Returns the
        new row's id. ``aggregation_kind`` must be one of
        ``{'intensive', 'extensive'}`` (enforced by the DB CHECK).
        """
        ...

    def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        """Return the layer row for a source_id (read from
        ``serving_geo.raster_layer_v``), or None. Used by the zonal-
        stats worker to read ``aggregation_kind`` + asset metadata
        (``storage_uri``, ``crs``, ``resolution_m``).
        """
        ...

    def get_by_id(self, layer_id: str) -> dict[str, Any] | None:
        """Return the layer row for a raster_layer id (read from
        ``serving_geo.raster_layer_v``), or None. Used by the recipe
        dispatcher to translate a recipe's ``input_raster_layer_id``
        (UI-picked) into the ``source_id`` the compute worker wants.
        ``get_by_source`` is the wrong shape here: a recipe binds to
        the layer, not the source.
        """
        ...

    def list_published_layers(self) -> list[dict[str, Any]]:
        """Return every ``serving_geo.raster_layer_v`` row where
        ``published=true``. Used by the tile-source resolver to emit a
        titiler URL per row. Compute paths read via ``get_by_source``
        instead — they must not be gated on ``published=true``.
        """
        ...

    def set_published(self, *, layer_slug: str, published: bool) -> None:
        """Flip ``published`` for the row with the given ``layer_slug``.
        Backs the ``reimagina raster publish`` / ``unpublish`` CLI.
        """
        ...


class AggregationRecipeRepository(Protocol):
    """Declarative recipes binding an input source + method + target grain.

    ``curated_geo.aggregation_recipe`` is the canonical write target
    (r020); read-side methods go through
    ``serving_geo.aggregation_recipe_v`` so workers honour the
    "API/workers read only serving_geo" rule.

    One recipe = "produce observations at ``target_feature_type_slug``
    under ``attribute_key_prefix`` via ``method`` from this input". The
    input is either a raster (``input_kind='raster'``) or the set of
    current observations at a finer grain (``input_kind='observation'``);
    the XOR is enforced by a DB CHECK.
    """

    def register_recipe(
        self,
        *,
        input_kind: str,
        target_feature_type_slug: str,
        attribute_key_prefix: str,
        method: str,
        input_raster_layer_id: str | None = None,
        input_attribute_key_prefix: str | None = None,
        input_feature_type_slug: str | None = None,
        weight_basis: str | None = None,
        params: dict[str, Any] | None = None,
        coverage_threshold: float = 0.0,
        aoi_id: str | None = None,
        enabled: bool = True,
    ) -> str:
        """Insert a new recipe row, return the new id. The DB enforces the
        XOR between raster / observation input shapes.
        """
        ...

    def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        """Return a single recipe (from ``serving_geo.aggregation_recipe_v``)
        or None. Dispatcher looks up by id and branches on ``input_kind``.
        """
        ...

    def list_recipes(
        self,
        *,
        input_raster_layer_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List recipes from ``serving_geo.aggregation_recipe_v``. Filter
        by raster layer for the common "run all recipes for layer X"
        command; ``enabled_only`` hides disabled rows so the dispatcher
        never runs them by accident.
        """
        ...

    def set_enabled(self, *, recipe_id: str, enabled: bool) -> None:
        """Flip ``enabled`` on one recipe. Parallel to
        ``RasterLayerRepository.set_published``.
        """
        ...

    def delete_recipe(self, recipe_id: str) -> None:
        """Hard-delete a recipe. Observations it produced stay put —
        operators retire them via ``retire_current_by_prefix`` when
        they decide the prefix is no longer wanted.
        """
        ...
