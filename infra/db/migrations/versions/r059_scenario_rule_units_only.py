"""A scenario rule says per number how it applies; the rule-wide mode goes.

r058 described a rule twice: ``mode`` (``set`` or ``change``) and, for a
change rule, ``units`` per number. Once one rule could set one number and
change another (``units`` value ``set``), ``mode`` said nothing ``units`` did
not, and every reader had to check both. Now ``units`` alone says it, per
number: ``set`` (the block's new value), ``abs`` (an amount) or ``pct``; a
number without one is ``set``, so an old set rule (``units = '{}'``) reads
unchanged. A change rule's numbers that had no unit were amounts, so they get
``abs`` written before ``mode`` is dropped.

Downgrade restores ``mode`` (``change`` when any number is not ``set``) and
drops the ``set`` entries from a change rule's units, which r058 cannot read;
a rule that set one number and changed another comes back as a change rule
that changes the other only.

Revision ID: r059
Revises: r058
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op

revision = "r059"
down_revision = "r058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE curated_geo.scenario_rule r
           SET units = (
                 SELECT jsonb_object_agg(k, COALESCE(r.units ->> k, 'abs'))
                   FROM jsonb_object_keys(r.inputs) AS k
               )
         WHERE r.mode = 'change'
        """
    )
    op.execute(
        """
        ALTER TABLE curated_geo.scenario_rule
          DROP COLUMN mode,
          ADD CONSTRAINT scenario_rule_units_check CHECK (
                jsonb_typeof(units) = 'object'
            AND NOT jsonb_path_exists(
                  units, '$.* ? (@ != "set" && @ != "abs" && @ != "pct")'
                )
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE curated_geo.scenario_rule
          DROP CONSTRAINT scenario_rule_units_check,
          ADD COLUMN mode text NOT NULL DEFAULT 'set'
              CHECK (mode IN ('set', 'change'))
        """
    )
    op.execute(
        """
        UPDATE curated_geo.scenario_rule
           SET mode = 'change',
               -- r058 would read a set number of a change rule as an amount.
               inputs = (
                 SELECT jsonb_object_agg(k, v)
                   FROM jsonb_each(inputs) AS i(k, v)
                  WHERE COALESCE(units ->> k, 'set') <> 'set'
               ),
               units = (
                 SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb)
                   FROM jsonb_each_text(units) AS e(k, v)
                  WHERE v <> 'set'
               )
         WHERE jsonb_path_exists(units, '$.* ? (@ != "set")')
        """
    )
    op.execute(
        """
        UPDATE curated_geo.scenario_rule
           SET units = '{}'::jsonb
         WHERE mode = 'set'
        """
    )
