"""Let an AOI declare membership by identifier instead of by geometry alone.

``compute_overlaps`` decided membership purely spatially, which is the only
option for data that carries no identifiers — Overture buildings, OSM
establishments — and the *wrong* option for data that does.

INEGI's marco geoestadístico gives every block, AGEB and municipality an exact
administrative address: ``CVE_ENT``, ``CVE_MUN``, ``CVE_AGEB``, ``CVE_MZA``. A
block is in Tlalpan iff ``CVE_ENT='09' AND CVE_MUN='012'`` — by definition, not
by measurement. Deciding it with ``ST_Intersects`` instead throws that certainty
away and reintroduces it as a question of degree. Measured on Tlalpan:

    grain          by CVE code   by geometry   code-only   geometry-only
    block                5,267         5,273           0               6
    ageb                   207           239           0              32
    municipality             1             1           0               0
    building                 0       217,873           0         217,873
    establishment            0         1,785           0           1,785

``code-only`` is zero at every grain, so geometry never misses a feature the
codes claim: it is a strict *superset*. The 6 blocks and 32 AGEBs it adds belong
to neighbouring alcaldías and merely cross the boundary. The spatial join was
manufacturing ambiguity the data does not contain — and the phantom-municipality
bug and the straddling-feature question are both downstream of that.

The last two rows are why this cannot simply replace the spatial path: buildings
and establishments have **no** codes, so geometry remains the only way to place
them. Hence a key rather than a switch.

**``membership_key`` is jsonb, not a pair of CVE columns.** Membership becomes
``f.attributes @> aoi.membership_key`` — one containment test, no vocabulary
baked into the schema. A state AOI declares ``{"CVE_ENT": "09"}``, an alcaldía
``{"CVE_ENT": "09", "CVE_MUN": "012"}``, and a future non-MEX source declares
whatever its own identifiers are without another migration. The column stays
NULL for AOIs that have no identifier to declare — a drawn study area, a
corridor — and those keep the geometry behaviour unchanged.

**Deliberately not a foreign key to the source feature.** Pointing at the
municipality row would be tighter provenance but makes membership depend on that
row surviving, and re-ingesting the marco geoestadístico replaces feature ids.
The codes outlive the rows; that is the entire reason to prefer them.

⚠ No backfill. The column is NULL for existing AOIs, which preserves today's
behaviour exactly rather than silently re-scoping anything already computed.
``aoi-from-alcaldia`` and ``aoi-from-state`` populate it for new AOIs, and an
existing AOI adopts it by being recreated or by an explicit UPDATE followed by
``aoi compute-features``.

⚠ No GIN index on ``feature.attributes``. The containment test runs only on
candidates already narrowed by the bbox-and-AOI geometry filter, so it is a
predicate over few rows rather than a lookup over all of them. Measure before
adding one — an index chosen on prediction is a write cost paid forever.

Revision ID: r040
Revises: r039
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "r040"
down_revision = "r039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "area_of_interest",
        sa.Column("membership_key", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="curated_geo",
    )
    op.execute(
        """
        COMMENT ON COLUMN curated_geo.area_of_interest.membership_key IS
        'Identifier-based membership, e.g. {"CVE_ENT":"09","CVE_MUN":"012"}. '
        'A feature carrying every key in here is a member iff its attributes '
        'contain these exact values, decided instead of the spatial overlap. '
        'Features lacking these keys (Overture buildings, OSM establishments) '
        'fall back to geometry. NULL means geometry decides for everything.'
        """
    )
    # Rejects an empty object, which would otherwise contain-match *every*
    # feature and silently make the whole dataset a member.
    op.create_check_constraint(
        "area_of_interest_membership_key_nonempty",
        "area_of_interest",
        "membership_key IS NULL OR jsonb_typeof(membership_key) = 'object' "
        "AND membership_key <> '{}'::jsonb",
        schema="curated_geo",
    )


def downgrade() -> None:
    op.drop_constraint(
        "area_of_interest_membership_key_nonempty",
        "area_of_interest",
        schema="curated_geo",
        type_="check",
    )
    op.drop_column("area_of_interest", "membership_key", schema="curated_geo")
