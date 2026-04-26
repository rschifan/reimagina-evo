"""Census rollup recipes — block → AGEB / municipality, sum.

Built on top of the r020 ``aggregation_recipe`` machinery so the
existing dispatcher (``jobs/derivations.run_recipe``) does the heavy
lifting. This module is a thin seed + runner: it declares one recipe
per (headline column × target grain) and runs all enabled recipes in
the family.

The output attribute keys follow ``censo_2010_<col>_<grain>``:

  censo_2010_pobtot          (block grain — already in observation)
  censo_2010_pobtot_ageb     (rolled up via this module)
  censo_2010_pobtot_municipality

So the choropleth picker can render the same demographic at any of
the three grains without colliding key names.

Why a sibling module to ``inegi_censo``: the ingest pipeline writes
block-grain observations from the census parquet — that's where the
data lake meets curated_geo. The rollup pipeline takes those existing
observations and produces *more* observations at coarser grains —
that's a derivation, no I/O against the data lake. Keeping them apart
mirrors the recipe layer's own input_kind ``observation`` arm.

Prerequisites the operator runs once:

  1. Block→AGEB and block→municipality overlap pairs declared (CLI:
     ``overlap declare-pair --source-context MEX ...``).
  2. ``feature_overlap`` rows computed for each pilot AOI (CLI:
     ``overlap compute-all --aoi-id <id>``).
  3. ``inegi-censo bootstrap`` populated the block-grain observations.

Then :func:`seed_recipes` + :func:`run_all` together produce the
AGEB and municipality rolled-up observations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from reimagina.jobs.derivations import RecipeRunResult, run_recipe
from reimagina.pipeline.inegi_censo import (
    ATTRIBUTE_KEY_PREFIX,
    HEADLINE_COLUMNS,
)
from reimagina.storage import backend_for_connection


logger = logging.getLogger(__name__)


# Block is the input grain (where the census ingest landed). AGEB and
# municipality are the two coarser grains we want the picker to be able
# to render.
INPUT_FEATURE_TYPE_SLUG = "block"
TARGET_GRAINS: tuple[str, ...] = ("ageb", "municipality")

# Census variables are extensive (population counts, dwelling counts) —
# they sum cleanly across child features. ``mean`` would be wrong for
# every headline column we ingest today.
ROLLUP_METHOD = "sum"
OVERLAP_BASIS = "area"


@dataclass(frozen=True)
class RollupSpec:
    """One (column, target_grain) recipe definition."""

    column: str
    target_feature_type_slug: str

    @property
    def input_attribute_key_prefix(self) -> str:
        """The block-grain attribute_key the ingest pipeline wrote."""
        return f"{ATTRIBUTE_KEY_PREFIX}_{self.column}"

    @property
    def output_attribute_key_prefix(self) -> str:
        """Adds the target grain so AGEB and municipality rollups don't
        collide on the same attribute_key for the same feature."""
        return f"{ATTRIBUTE_KEY_PREFIX}_{self.column}_{self.target_feature_type_slug}"


@dataclass(frozen=True)
class SeedResult:
    """Outcome of :func:`seed_recipes`."""

    created_recipe_ids: tuple[str, ...]
    reused_recipe_ids: tuple[str, ...]
    skipped: tuple[RollupSpec, ...] = field(default_factory=tuple)


# ─── Specs ─────────────────────────────────────────────────────────────


def specs(
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    target_grains: tuple[str, ...] = TARGET_GRAINS,
) -> list[RollupSpec]:
    """Cartesian product of headline columns × target grains. Override
    via the kwargs in tests or for partial seeds."""
    return [
        RollupSpec(column=col, target_feature_type_slug=grain)
        for col in columns
        for grain in target_grains
    ]


# ─── Seed ──────────────────────────────────────────────────────────────


def _existing_recipe_id(
    backend, *, target_feature_type_slug: str, attribute_key_prefix: str,
) -> str | None:
    """Look up an existing recipe by the (target, output_prefix) UNIQUE
    pair in r020. Returns None if not present."""
    for recipe in backend.aggregation_recipe_repo().list_recipes(enabled_only=False):
        if (
            recipe["target_feature_type_slug"] == target_feature_type_slug
            and recipe["attribute_key_prefix"] == attribute_key_prefix
        ):
            return str(recipe["id"])
    return None


def seed_recipes(
    conn,
    *,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    target_grains: tuple[str, ...] = TARGET_GRAINS,
) -> SeedResult:
    """Idempotently register one recipe per (column × target_grain).

    Idempotency keys off the r020 UNIQUE on
    ``(target_feature_type_slug, attribute_key_prefix)``. Re-running
    this function reuses every existing row instead of insert-erroring.
    """
    backend = backend_for_connection(conn)
    repo = backend.aggregation_recipe_repo()

    created: list[str] = []
    reused: list[str] = []

    for spec in specs(columns, target_grains):
        existing = _existing_recipe_id(
            backend,
            target_feature_type_slug=spec.target_feature_type_slug,
            attribute_key_prefix=spec.output_attribute_key_prefix,
        )
        if existing is not None:
            reused.append(existing)
            continue

        recipe_id = repo.register_recipe(
            input_kind="observation",
            input_attribute_key_prefix=spec.input_attribute_key_prefix,
            input_feature_type_slug=INPUT_FEATURE_TYPE_SLUG,
            target_feature_type_slug=spec.target_feature_type_slug,
            attribute_key_prefix=spec.output_attribute_key_prefix,
            method=ROLLUP_METHOD,
            params={"overlap_basis": OVERLAP_BASIS},
            enabled=True,
        )
        created.append(recipe_id)

    conn.commit()
    return SeedResult(
        created_recipe_ids=tuple(created),
        reused_recipe_ids=tuple(reused),
    )


# ─── Run ───────────────────────────────────────────────────────────────


def list_census_recipe_ids(
    conn,
    *,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    target_grains: tuple[str, ...] = TARGET_GRAINS,
    enabled_only: bool = True,
) -> list[str]:
    """Recipe ids for every (column × grain) we manage. Skip rows that
    haven't been seeded yet (caller decides whether that's a bug)."""
    backend = backend_for_connection(conn)
    expected = {
        (spec.target_feature_type_slug, spec.output_attribute_key_prefix)
        for spec in specs(columns, target_grains)
    }
    matches: list[str] = []
    for row in backend.aggregation_recipe_repo().list_recipes(enabled_only=enabled_only):
        if (row["target_feature_type_slug"], row["attribute_key_prefix"]) in expected:
            matches.append(str(row["id"]))
    return matches


def run_all(
    conn,
    *,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    target_grains: tuple[str, ...] = TARGET_GRAINS,
    direct_dsn: str | None = None,
) -> list[RecipeRunResult]:
    """Run every census-family rollup recipe found in the catalog.

    Recipes are sequential: parallel execution would contend on the
    output-prefix advisory lock anyway when two recipes target the
    same prefix, and per-recipe wall time is small enough that a
    sequential loop keeps the dispatcher logs linear and easy to read.
    """
    recipe_ids = list_census_recipe_ids(
        conn, columns=columns, target_grains=target_grains, enabled_only=True,
    )
    if not recipe_ids:
        logger.warning(
            "No census rollup recipes found; run seed_recipes() first.",
        )
    results: list[RecipeRunResult] = []
    for rid in recipe_ids:
        result = run_recipe(conn, recipe_id=rid, direct_dsn=direct_dsn)
        results.append(result)
    return results


# ─── Diagnostics for CLI / tests ──────────────────────────────────────


@dataclass(frozen=True)
class ApplyReport:
    """Combined seed + run summary for the CLI."""

    seeded_created: int
    seeded_reused: int
    recipes_run: int
    observations_written: int
    per_recipe: tuple[dict[str, Any], ...]


def apply(
    conn,
    *,
    columns: tuple[str, ...] = HEADLINE_COLUMNS,
    target_grains: tuple[str, ...] = TARGET_GRAINS,
    direct_dsn: str | None = None,
) -> ApplyReport:
    """Convenience: seed + run in one call. The CLI's primary entry point."""
    seed_result = seed_recipes(
        conn, columns=columns, target_grains=target_grains,
    )
    run_results = run_all(
        conn, columns=columns, target_grains=target_grains, direct_dsn=direct_dsn,
    )
    return ApplyReport(
        seeded_created=len(seed_result.created_recipe_ids),
        seeded_reused=len(seed_result.reused_recipe_ids),
        recipes_run=len(run_results),
        observations_written=sum(r.observations_written for r in run_results),
        per_recipe=tuple(
            {
                "recipe_id": r.recipe_id,
                "attribute_key_prefix": r.attribute_key_prefix,
                "observations_written": r.observations_written,
            }
            for r in run_results
        ),
    )
