"""data_source_catalog.credit: the map credit a source's licence prescribes.

INEGI's terms ask for one credit per product — "Fuente: INEGI, <producto>,
<fecha>" — and the map credited one generic line per (provider, licence), so
the Marco Geoestadístico, the census and DENUE shared a single INEGI line.
Loaders now record the product line here; NULL keeps the provider-and-licence
fallback (``reimagina.data.attribution``). Rows already loaded are backfilled
by the (provider, version) / name the INEGI loaders register.

Revision ID: r054
Revises: r053
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "r054"
down_revision = "r053"
branch_labels = None
depends_on = None

# (credit, WHERE clause) — the values the loaders register, as of this revision.
_BACKFILL = (
    (
        "Fuente: INEGI, Marco Geoestadístico 2024",
        "provider = 'INEGI' AND version = '2024.01'",
    ),
    (
        "Fuente: INEGI, Marco Geoestadístico 2020",
        "provider = 'INEGI' AND version = '2020.1'",
    ),
    (
        "Fuente: INEGI, Censo de Población y Vivienda 2020",
        "provider = 'INEGI' AND version = '2020-censo'",
    ),
    (
        "Fuente: INEGI, Directorio Estadístico Nacional de Unidades Económicas "
        "(DENUE), 05/2025",
        "provider = 'INEGI' AND name = 'Mexicali · Amenities (INEGI DENUE)' "
        "AND version = '2025-05'",
    ),
)


def upgrade() -> None:
    op.execute("ALTER TABLE curated_geo.data_source_catalog ADD COLUMN credit text")
    op.execute(
        "COMMENT ON COLUMN curated_geo.data_source_catalog.credit IS "
        "'Exact map credit line the source''s licence prescribes (e.g. INEGI: "
        "Fuente: INEGI, <producto>, <fecha>). NULL = provider (license) fallback.'"
    )
    for credit, where in _BACKFILL:
        op.execute(
            f"UPDATE curated_geo.data_source_catalog SET credit = '{credit}' "
            f"WHERE {where}"
        )


def downgrade() -> None:
    op.execute("ALTER TABLE curated_geo.data_source_catalog DROP COLUMN credit")
