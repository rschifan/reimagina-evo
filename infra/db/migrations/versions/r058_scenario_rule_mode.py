"""A scenario rule sets values or changes them.

``mode`` is ``'set'`` (the rule's numbers are the blocks' new values, as every
rule before this was) or ``'change'`` (each number moves a block from its own
baseline value). ``units`` says, for a change rule, whether each number is an
amount (``'abs'``) or a percentage (``'pct'``); a set rule has none. Existing
rows are set rules. The arithmetic is done when the scenario is materialised
(``authoring._resolve_rules``), so overrides, tiles and comparisons still hold
absolute values.

Revision ID: r058
Revises: r057
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op

revision = "r058"
down_revision = "r057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE curated_geo.scenario_rule
          ADD COLUMN mode text NOT NULL DEFAULT 'set'
              CHECK (mode IN ('set', 'change')),
          ADD COLUMN units jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE curated_geo.scenario_rule DROP COLUMN units, DROP COLUMN mode"
    )
