"""State the overlay tiles' real zoom floor in their comments.

r025 lowered both overlay functions' floor to z8 (``z >= 8``), but their
comments still said "Returns empty below z=10", and the generated database
reference repeats a function's comment. Comments only; no behaviour changes.

Revision ID: r052
Revises: r051
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision = "r052"
down_revision = "r051"
branch_labels = None
depends_on = None

_SIGNATURE = "(integer, integer, integer, json)"

_INDICATOR = (
    "Martin function source — MVT choropleth for one indicator. Required: "
    "indicator_slug. Optional: scenario_id, feature_type, aoi_id (aoi_id "
    "filters features to that AOI's membership). Returns empty below z={z}."
)
_AGGREGATION = (
    "Martin function source — MVT choropleth for aggregation-recipe "
    "observations. Required: attribute_key, feature_type_slug. Optional: "
    "aoi_id (filters features to that AOI's membership). Returns empty below "
    "z={z}."
)


def _comment(z: int) -> None:
    for name, text in (
        ("indicator_overlay_tile", _INDICATOR),
        ("aggregation_overlay_tile", _AGGREGATION),
    ):
        body = text.format(z=z).replace("'", "''")
        op.execute(f"COMMENT ON FUNCTION serving_geo.{name}{_SIGNATURE} IS '{body}'")


def upgrade() -> None:
    _comment(8)


def downgrade() -> None:
    _comment(10)
