"""Retire the single ``scenario.override`` asset — it is now one per attribute.

r041 registered one override asset for every attribute at once, so every
attribute-reading indicator was a consumer of every edit. Publishing a version
for an edit to ``floors`` bumped the one version all thirteen read, and all
thirteen went out of date: the flat-union over-approximation
``resolve_change_set`` documents, restated as a graph.

``assets.registry.scenario_override_assets`` now projects one asset per
overridable attribute — ``scenario.override.floors``,
``scenario.override.num_levels``, … — each partitioned by scenario exactly as
the old one was. An edit publishes a version for one of them, and the downstream
propagation in ``asset_component_v`` carries the scenario partition only to the
indicators that declared an edge to that attribute. No new partition semantics:
the existing ``identity`` mapping does it, unchanged.

This migration only removes what the registry no longer projects. The new assets
arrive from **code**, not from a migration body — the registry has been seeded
by ``seed_assets`` since r041, deliberately, because per-migration catalog
inserts are what drifted ``explore.layer_catalog`` far enough to need
``scripts/repair_layer_catalog.sql``.

⚠ Between this migration and the next ``seed_assets``, scenario submits plan
nothing: the indicators have no override edges, so no scenario partition
propagates and the dirty set is empty. That is a visible noop rather than a
wrong answer, it is what ``db/bootstrap`` runs on every startup, and the
alternative — writing the new registry rows here — is the drift this design
already refused once.

Order matters. ``asset_dependency.producer_asset_id`` is ON DELETE RESTRICT, so
the edges pointing at the retired asset must go first; its ``asset_version``
rows are ON DELETE CASCADE and go with it.

The downgrade restores the asset row so ``r041``'s shape is recoverable, but not
its version history — retired versions are not resurrectable and a re-seed
republishes from the override rows anyway.

Revision ID: r045
Revises: r044
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "r045"
down_revision = "r044"
branch_labels = None
depends_on = None

_RETIRED = "scenario.override"


def upgrade() -> None:
    op.execute(f"""
        DELETE FROM curated_geo.asset_dependency
        WHERE producer_asset_id IN (
            SELECT id FROM curated_geo.asset WHERE asset_key = '{_RETIRED}'
        )
    """)
    op.execute(f"""
        DELETE FROM curated_geo.asset WHERE asset_key = '{_RETIRED}'
    """)


def downgrade() -> None:
    op.execute(f"""
        INSERT INTO curated_geo.asset
          (asset_key, kind, backing_kind, backing_ref, version_strategy,
           partition_dimensions, description)
        VALUES ('{_RETIRED}', 'source', 'scenario_override',
                'curated_geo.scenario_override', 'commit', ARRAY['scenario'],
                'Per-scenario attribute overrides — the what-if input')
        ON CONFLICT (asset_key) DO NOTHING
    """)
