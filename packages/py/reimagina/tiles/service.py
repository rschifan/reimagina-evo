"""Compatibility surface for the tile modules.

The two halves now live apart: :mod:`reimagina.tiles.export` shells out to GDAL
to write a PMTiles snapshot, :mod:`reimagina.tiles.sources` queries Postgres to
describe which layers exist. They shared no state, so keeping them in one
module only meant two dependency sets and two failure modes behind one import.

Only the public entry points are re-exported here. Anything private belongs to
the module that owns it — including for tests, which must patch the name in the
defining module: `export_pmtiles_snapshot` resolves `_run_export_command`
through `tiles.export`'s namespace, so rebinding it here would have no effect.
"""

from __future__ import annotations

from reimagina.tiles.export import (
    default_pmtiles_output_dir as default_pmtiles_output_dir,
    export_pmtiles_snapshot as export_pmtiles_snapshot,
)
from reimagina.tiles.sources import resolve_tile_sources as resolve_tile_sources
