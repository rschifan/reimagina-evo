"""The asset graph: one dependency graph over everything the platform produces.

See ``infra/db/migrations/versions/r041_asset_graph.py`` for the schema and the
reasoning. In short: every named artifact is an asset, a source asset's version
is recorded by whatever changed it, a derived asset's version is a hash of its
upstream closure, and "stale" is ``materialized <> expected`` — computed on read
rather than marked by a trigger somebody has to remember to fire.

⚠ This module deliberately re-exports nothing. ``indicators.catalog`` imports
``assets.keys`` to declare its edges, and ``assets.registry`` imports
``indicators.catalog`` to project indicator assets out of them. Pulling
``registry`` in here would close that into an import cycle. Import the submodule
you need.
"""

from __future__ import annotations
