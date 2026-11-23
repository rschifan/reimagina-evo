"""The indicator overlay tile says which blocks' inputs are estimated.

``scripts/seed_demo_attributes.py --fit-to-census`` has to guess the lot
coverage of a block with census dwellings where Overture maps none of its
buildings, or only a sliver (under 20 m² per dwelling), and marks it
``attributes.estimated`` = ``no_footprint`` / ``thin_footprint``. Both views
of ``indicator_overlay_tile`` (values and delta) now emit that as an
``estimated`` property, so a tooltip can say a block's number rests on a
guess. ``ST_AsMVT`` omits a NULL property, so an unflagged block carries
none.

r049's function, with one column added to each branch: the body is built by
r049's own builders (loaded from its file, since migration modules are not a
package) rather than copied, so the anti-join and the delta classification
cannot drift from the definition r049 tested. Downgrade reinstalls r049's
function exactly.

Also corrects ``serving_geo.get_tile``'s comment, which r010 wrote and no
later migration updated: it listed ``type_slug, role`` and not r039's
``aoi_id``. Downgrade puts r010's text back.

Revision ID: r056
Revises: r055
Create Date: 2026-11-22
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "r056"
down_revision = "r055"
branch_labels = None
depends_on = None

_ESTIMATED = ",\n          f.attributes->>'estimated' AS estimated"


def _r049():
    path = Path(__file__).with_name("r049_indicator_overlay_delta.py")
    spec = importlib.util.spec_from_file_location("_r049_overlay", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body_with_estimated() -> str:
    r049 = _r049()
    body = r049._body(r049._OVERLAY, delta=True)
    for anchor in (
        "ir.quality_flag         AS quality_flag",
        "END                     AS state",
    ):
        # Exactly once each: a second match would mean r049's text moved and
        # the column landed somewhere it should not.
        assert body.count(anchor) == 1, anchor
        body = body.replace(anchor, anchor + _ESTIMATED)
    return body


_GET_TILE_SIGNATURE = "serving_geo.get_tile(integer, integer, integer, json)"

_GET_TILE_COMMENT = """'Martin function source — returns a Mapbox Vector Tile for the requested z/x/y.
Optional query params: type_slug, role, aoi_id (a uuid; blank means unscoped, anything else matches nothing).
Geometry is simplified at z < 14, buildings appear from z 14, and attributes are omitted below z 12 to keep tiles small.'"""

# r010's text, verbatim, for the downgrade.
_R010_GET_TILE_COMMENT = """'Martin function source — returns a Mapbox Vector Tile for the requested z/x/y.
Optional query params: type_slug, role.
Geometry is simplified at z < 14 and attributes are omitted below z 12 to keep tiles small.'"""


def upgrade() -> None:
    op.execute(_body_with_estimated())
    op.execute(f"COMMENT ON FUNCTION {_GET_TILE_SIGNATURE} IS {_GET_TILE_COMMENT}")


def downgrade() -> None:
    op.execute(f"COMMENT ON FUNCTION {_GET_TILE_SIGNATURE} IS {_R010_GET_TILE_COMMENT}")
    r049 = _r049()
    op.execute(r049._body(r049._OVERLAY, delta=True))
