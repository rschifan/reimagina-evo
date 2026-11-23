"""Serve road features only from z13.

Roads carry ``role = 'infrastructure'``, which ``get_tile``'s role gate serves
from z12. On the Mexicali pilot (``scripts/mexicali_run.sh``, 117,858 road
features) a central road tile measured 2.2 MB at z12 — the order of the building
tile r038 floored at z14 — and 0.96 MB at z13. The remedy is r038's: a
``type_slug`` floor, on the same axis and for the same reason (role cannot be
used: municipalities and buildings share ``context``, and only roads are
``infrastructure`` today, but a rendering budget is a property of the layer,
not of the role). The client layer's ``minzoom`` follows (``apps/web/src/
layers.ts``), so it never asks for a tile that can only be empty.

r039's function, with one predicate added beside r038's building floor: the
body is r039's own (loaded from its file, since migration modules are not a
package) rather than copied, so the AOI filter cannot drift from the
definition r039 tested. Downgrade reinstalls r039's function and r056's
comment exactly.

Revision ID: r057
Revises: r056
Create Date: 2026-11-23
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "r057"
down_revision = "r056"
branch_labels = None
depends_on = None

#: The zoom road features start at. Pinned against the web layer's
#: ``minzoom`` by tests/contract/test_tile_zoom_floors.py.
ROAD_MIN_ZOOM = 13

_BUILDING_FLOOR = "AND (z >= 14 OR f.type_slug <> 'building')"
_ROAD_FLOOR = f"\n      AND (z >= {ROAD_MIN_ZOOM} OR f.type_slug <> 'road')"

_SIGNATURE = "serving_geo.get_tile(integer, integer, integer, json)"

_COMMENT = """'Martin function source — returns a Mapbox Vector Tile for the requested z/x/y.
Optional query params: type_slug, role, aoi_id (a uuid; blank means unscoped, anything else matches nothing).
Geometry is simplified at z < 14, roads appear from z 13, buildings from z 14, and attributes are omitted below z 12 to keep tiles small.'"""


def _load(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _r039_body() -> str:
    r039 = _load("r039_get_tile_aoi_filter.py")
    return r039._GET_TILE.format(aoi_filter=r039._AOI_FILTER)


def get_tile_body() -> str:
    body = _r039_body()
    # Exactly once: a second match would mean r039's text moved and the floor
    # landed somewhere it should not.
    assert body.count(_BUILDING_FLOOR) == 1
    return body.replace(_BUILDING_FLOOR, _BUILDING_FLOOR + _ROAD_FLOOR)


def upgrade() -> None:
    op.execute(get_tile_body())
    op.execute(f"COMMENT ON FUNCTION {_SIGNATURE} IS {_COMMENT}")


def downgrade() -> None:
    op.execute(_r039_body())
    r056 = _load("r056_overlay_tile_estimated.py")
    op.execute(f"COMMENT ON FUNCTION {_SIGNATURE} IS {r056._GET_TILE_COMMENT}")
