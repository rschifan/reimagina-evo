"""Mark an indicator bound that means "beyond measurement".

``cap_value`` is the value an indicator is clamped to (the accessibility
distances at their cutoff); a reading at the cap means "at least this", not a
measurement. NULL for every indicator that is not clamped. Distinct from
``valid_min``/``valid_max``, which are validation ranges.

Revision ID: r051
Revises: r050
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision = "r051"
down_revision = "r050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE curated_geo.indicator_catalog ADD COLUMN cap_value double precision"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE curated_geo.indicator_catalog DROP COLUMN cap_value")
