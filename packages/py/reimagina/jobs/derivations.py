"""Dispatcher for declarative aggregation recipes (r020).

One entry point: :func:`run_recipe`. Reads a row from
``serving_geo.aggregation_recipe_v`` and routes to the right worker
based on ``input_kind``:

- ``'raster'`` → thin wrapper over
  :func:`reimagina.jobs.raster_zonal_stats.run_raster_zonal_stats`
  (maps ``method`` + ``params['stats']`` into the worker's
  ``statistics`` argument).
- ``'observation'`` → thin wrapper over
  :func:`reimagina.jobs.observation_rollup.run_observation_rollup`.

Both arms share the same observation SCD retire+upsert contract, so
recipes writing to the same output prefix serialize via the same
advisory-lock mechanism regardless of which arm runs.

The dispatcher is intentionally thin — it exists to translate recipe
columns into worker kwargs and nothing else. All business logic stays
in the two workers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reimagina.jobs.observation_rollup import (
    ObservationRollupResult,
    run_observation_rollup,
)
from reimagina.jobs.raster_zonal_stats import (
    ZonalStatsResult,
    run_raster_zonal_stats,
)
from reimagina.storage import backend_for_connection


class UnknownRecipe(ValueError):
    """Raised when the recipe id doesn't resolve to a row."""


class DisabledRecipe(ValueError):
    """Raised when a recipe is disabled — dispatcher refuses to run it."""


@dataclass(frozen=True)
class RecipeRunResult:
    """Normalised outcome across both arms.

    Exposes only the fields a CLI / log reader cares about. The
    arm-specific result object is preserved on ``inner`` for callers
    that need ``features_in_scope`` / ``targets_written`` etc.
    """

    recipe_id: str
    input_kind: str
    ingestion_run_id: str
    attribute_key_prefix: str
    observations_written: int
    inner: ZonalStatsResult | ObservationRollupResult


def run_recipe(conn, *, recipe_id: str, direct_dsn: str | None = None) -> RecipeRunResult:
    """Fetch the recipe, dispatch to the right arm, normalise the result.

    ``direct_dsn`` is forwarded to both workers — required on Postgres
    where the critical section opens a dedicated transaction.
    """
    backend = backend_for_connection(conn)
    recipe = backend.aggregation_recipe_repo().get_recipe(recipe_id)
    if recipe is None:
        raise UnknownRecipe(f"No aggregation_recipe row with id={recipe_id!r}")
    if not recipe.get("enabled"):
        raise DisabledRecipe(
            f"Recipe {recipe_id!r} is disabled; enable before running"
        )

    if recipe["input_kind"] == "raster":
        return _run_raster_recipe(conn, recipe, direct_dsn=direct_dsn)
    if recipe["input_kind"] == "observation":
        return _run_observation_recipe(conn, recipe, direct_dsn=direct_dsn)
    raise ValueError(f"Unknown input_kind: {recipe['input_kind']!r}")


def run_all_for_raster(
    conn,
    *,
    raster_layer_id: str,
    direct_dsn: str | None = None,
) -> list[RecipeRunResult]:
    """Run every enabled recipe whose input is the given raster layer.

    Used by ``reimagina raster derivation run --layer-slug X`` and by
    an optional post-ingest hook. Recipes run sequentially — parallel
    execution would contend on the prefix lock anyway if two target
    the same output prefix.
    """
    backend = backend_for_connection(conn)
    recipes = backend.aggregation_recipe_repo().list_recipes(
        input_raster_layer_id=raster_layer_id,
        enabled_only=True,
    )
    return [
        run_recipe(conn, recipe_id=r["id"], direct_dsn=direct_dsn)
        for r in recipes
    ]


# ─── Arm wrappers ─────────────────────────────────────────────────────


def _run_raster_recipe(
    conn, recipe: dict[str, Any], *, direct_dsn: str | None,
) -> RecipeRunResult:
    # The raster worker takes ``source_id`` (not layer_id). Derive the
    # raster's source_id from the layer row so recipes keep referring
    # to layer_id — the more stable, human-picked identifier.
    backend = backend_for_connection(conn)
    layer = backend.raster_layer_repo().get_by_id(recipe["input_raster_layer_id"])
    if layer is None:
        raise UnknownRecipe(
            f"Recipe {recipe['id']!r} references missing raster_layer "
            f"{recipe['input_raster_layer_id']!r}"
        )

    # Translate recipe.method + params.stats into the worker's
    # ``statistics`` tuple. If params.stats isn't set, use a
    # method-implied default so a recipe authored with just method='sum'
    # still produces exactly one stat.
    params = recipe.get("params") or {}
    stats = params.get("stats")
    if stats is None:
        stats = (_METHOD_TO_RASTERSTAT[recipe["method"]],)
    else:
        stats = tuple(stats)

    inner = run_raster_zonal_stats(
        conn,
        source_id=layer["source_id"],
        feature_type_slug=recipe["target_feature_type_slug"],
        attribute_key_prefix=recipe["attribute_key_prefix"],
        aoi_id=recipe.get("aoi_id"),
        coverage_threshold=float(recipe.get("coverage_threshold", 0.5)),
        statistics=stats,
        direct_dsn=direct_dsn,
    )
    return RecipeRunResult(
        recipe_id=str(recipe["id"]),
        input_kind="raster",
        ingestion_run_id=inner.ingestion_run_id,
        attribute_key_prefix=inner.attribute_key_prefix,
        observations_written=inner.observations_written,
        inner=inner,
    )


def _run_observation_recipe(
    conn, recipe: dict[str, Any], *, direct_dsn: str | None,
) -> RecipeRunResult:
    inner = run_observation_rollup(
        conn,
        input_attribute_key_prefix=recipe["input_attribute_key_prefix"],
        input_feature_type_slug=recipe["input_feature_type_slug"],
        target_feature_type_slug=recipe["target_feature_type_slug"],
        output_attribute_key_prefix=recipe["attribute_key_prefix"],
        method=recipe["method"],
        weight_basis=recipe.get("weight_basis"),
        aoi_id=recipe.get("aoi_id"),
        direct_dsn=direct_dsn,
        recipe_id=str(recipe["id"]),
    )
    return RecipeRunResult(
        recipe_id=str(recipe["id"]),
        input_kind="observation",
        ingestion_run_id=inner.ingestion_run_id,
        attribute_key_prefix=inner.attribute_key_prefix,
        observations_written=inner.observations_written,
        inner=inner,
    )


# ─── Helpers ──────────────────────────────────────────────────────────


# Maps the recipe ``method`` to the single rasterstats primitive the
# raster worker should request when ``params.stats`` is unset. Only the
# methods that have a direct pixel-level analogue are listed;
# ``weighted_mean`` has no pixel equivalent (weights live in a separate
# raster) and would need an explicit stats override via params.
_METHOD_TO_RASTERSTAT: dict[str, str] = {
    "mean": "mean",
    "sum": "sum",
    "min": "min",
    "max": "max",
}


