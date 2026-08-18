"""Pin the expected-version hash to byte order, not to the database's collation.

``serving_geo.asset_expected_version_v`` hashes
``string_agg(DISTINCT part, '|' ORDER BY part)``. That ORDER BY runs under the
column's collation, which is the database's default — ``en_US.UTF-8`` on every
machine this has run on. Its Python twin, ``reimagina.assets.version``, sorts
with ``sorted()``, which is byte order. Two orderings, one hash.

They agree today. Measured on the shipped registry: of 38 ``(asset, partition)``
nodes, **0** hash differently under the two orderings. This migration is not
fixing a live bug — it is retiring a hazard class, permanently, for one line.

The hazard is real because every component string is punctuation-dense —
``indicator.urban_form_block_compactness@scenario=S1=code:1:params:e481…``,
``edge:a->b:self:floors::0.500000:identity`` — and glibc and ICU both weaken or
ignore ``.``, ``_``, ``:``, ``-`` and ``=`` at the primary comparison level,
where byte order does not. Whether the two orderings coincide therefore depends
on which asset keys happen to be registered. A new indicator whose slug differs
from a neighbour's only in punctuation, a locale change, a glibc upgrade
(2.28 famously reordered), a restore into an ICU-default cluster: any of those
arms it, and none of them touch this repository.

What arming it looks like is why it is worth a migration. The two halves do not
crash — each stays internally consistent while disagreeing about what is out of
date, so the parity test in ``tests/integration/test_asset_graph.py`` starts
failing on a machine nobody changed anything on, and the platform's answer to
"what must I recompute" quietly depends on where it is running.

⚠ ``COLLATE "C"`` has to appear on the aggregate's *argument* as well as its
ORDER BY. Postgres rejects the ORDER BY alone —

    ERROR:  in an aggregate with DISTINCT, ORDER BY expressions must appear in
    argument list

— because it matches the two expressions syntactically. Collating the argument
does not change the value being hashed: ``COLLATE`` affects comparison and
sorting only, never the text itself, so ``md5`` sees exactly the same input.

Revision ID: r044
Revises: r043
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r044"
down_revision = "r043"
branch_labels = None
depends_on = None


def _body(aggregate: str) -> str:
    return f"""
    CREATE OR REPLACE VIEW serving_geo.asset_expected_version_v AS
    SELECT
        asset_id,
        asset_key,
        partition_key,
        -- DISTINCT is load-bearing, not tidiness. In a diamond
        -- (composite -> compactness -> block, composite -> density -> block)
        -- the same node is reachable by more than one path; where those paths
        -- differ in length the recursion emits it once per depth, and without
        -- DISTINCT the hash would depend on the *shape* of the graph rather
        -- than on its content. It would still be deterministic, so nothing
        -- would look wrong — it would just make the Python twin reproduce
        -- path multiplicity to agree. A set of components has neither problem.
        --
        -- COLLATE "C" is the same argument applied to *order*: byte order is
        -- the one ordering the Python twin's `sorted()` can reproduce on any
        -- machine. See this migration's docstring.
        --
        -- ...and COLLATE "default" back on the way out. A collation propagates
        -- through md5() to the output column, and CREATE OR REPLACE VIEW refuses
        -- to change a column's collation ("cannot change collation of view
        -- column"). Restoring it keeps the view's signature byte-identical, so
        -- asset_freshness_v on top of it never has to be dropped.
        md5({aggregate}) COLLATE "default" AS expected_version
    FROM serving_geo.asset_component_v
    GROUP BY asset_id, asset_key, partition_key
"""


def upgrade() -> None:
    op.execute(
        _body('string_agg(DISTINCT part COLLATE "C", \'|\' ORDER BY part COLLATE "C")')
    )


def downgrade() -> None:
    op.execute(_body("string_agg(DISTINCT part, '|' ORDER BY part)"))
