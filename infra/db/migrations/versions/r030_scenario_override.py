"""Scenario overrides — the what-if attribute-edit store.

``curated_geo.scenario_override`` holds a scenario's attribute edits, keyed like
the ``observation`` one-current-per-(feature, attribute) shape. Effective value
for a scenario = override ⊕ current observation ⊕ base ``feature.attributes``.
scenario_id is a plain uuid (no FK — the app_core.scenario entity is deferred),
matching the existing scenario_id convention.

Revision ID: r030
Revises: r029
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "r030"
down_revision = "r029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE curated_geo.scenario_override (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            scenario_id   uuid        NOT NULL,
            feature_id    uuid        NOT NULL
                                      REFERENCES curated_geo.feature(id) ON DELETE CASCADE,
            attribute_key text        NOT NULL,
            value_numeric double precision,
            value_text    text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (scenario_id, feature_id, attribute_key),
            CHECK (num_nonnulls(value_numeric, value_text) = 1)
        )
    """)
    op.execute(
        "CREATE INDEX scenario_override_lookup_idx "
        "ON curated_geo.scenario_override (scenario_id, attribute_key)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curated_geo.scenario_override")
