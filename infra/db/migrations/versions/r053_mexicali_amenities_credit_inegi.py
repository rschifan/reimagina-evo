"""Credit the Mexicali amenity source to INEGI, not IMIP.

The file ``points bootstrap --layers mexicali_amenities`` loads is INEGI DENUE
(every row's ``fuente`` is DENUE), but its ``data_source_catalog`` row was
registered as provider "IMIP Mexicali" — and, before the INEGI licence was set,
"Datos abiertos del IMIP" — so the map credited the wrong provider and licence.
The loader now registers it as INEGI. This relabels rows already loaded, which
also stops the next bootstrap from registering a second source under the new
name and loading the 1,493 points again beside the old ones.

Revision ID: r053
Revises: r052
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "r053"
down_revision = "r052"
branch_labels = None
depends_on = None

_OLD_NAME = "Mexicali · Amenities (IMIP / DENUE)"
_OLD_PROVIDER = "IMIP Mexicali"
_NEW_NAME = "Mexicali · Amenities (INEGI DENUE)"
_NEW_PROVIDER = "INEGI"
_INEGI_LICENSE = "Libre uso — Términos de Uso de la Información del INEGI"


def upgrade() -> None:
    # NOT EXISTS: UNIQUE (provider, name, version) — skip a version that a
    # bootstrap already registered under the new label.
    op.execute(
        f"""
        UPDATE curated_geo.data_source_catalog s
        SET name = '{_NEW_NAME}', provider = '{_NEW_PROVIDER}',
            license = '{_INEGI_LICENSE}'
        WHERE s.name = '{_OLD_NAME}' AND s.provider = '{_OLD_PROVIDER}'
          AND NOT EXISTS (
              SELECT 1 FROM curated_geo.data_source_catalog n
              WHERE n.name = '{_NEW_NAME}' AND n.provider = '{_NEW_PROVIDER}'
                AND n.version = s.version
          )
        """
    )


def downgrade() -> None:
    # The licence is left as INEGI's: the old value was wrong.
    op.execute(
        f"""
        UPDATE curated_geo.data_source_catalog s
        SET name = '{_OLD_NAME}', provider = '{_OLD_PROVIDER}'
        WHERE s.name = '{_NEW_NAME}' AND s.provider = '{_NEW_PROVIDER}'
          AND NOT EXISTS (
              SELECT 1 FROM curated_geo.data_source_catalog o
              WHERE o.name = '{_OLD_NAME}' AND o.provider = '{_OLD_PROVIDER}'
                AND o.version = s.version
          )
        """
    )
