"""Align aggregation_recipe.coverage_threshold's default with the worker's.

The same nominal computation produced different data depending on which entry
point invoked it. ``run_raster_zonal_stats`` defaults the threshold to **0.0**
and documents why: under exactextract's area weighting, writing a row for every
polygon that intersects any non-nodata cell is the cheap, conservative answer,
and the previous 0.5 "silently dropped manzanas smaller than the raster pixel
and is no longer recommended".

Every other entry point kept the 0.5 — this column's default, the recipe
repository, the storage Protocol, the dispatcher fallback and the CLI. So a
zonal-stats run invoked directly and the same run invoked through a recipe
disagreed about which small blocks exist, with nothing recording which path had
produced a given observation.

⚠ **Existing rows are deliberately left alone.** A stored 0.5 cannot be told
apart from a deliberate 0.5, and rewriting it would silently change results for
an operator who chose it. Recipes created from here on take 0.0; existing ones
keep whatever they hold until edited. ``explore.aggregation_recipe_v`` exposes
the column, so the ones still on the old value are one query away.

Revision ID: r042
Revises: r041
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r042"
down_revision = "r041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE curated_geo.aggregation_recipe "
        "ALTER COLUMN coverage_threshold SET DEFAULT 0.0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE curated_geo.aggregation_recipe "
        "ALTER COLUMN coverage_threshold SET DEFAULT 0.5"
    )
