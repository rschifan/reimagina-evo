"""The routing network — OSM ways and nodes, shaped for pgRouting.

Network accessibility needs shortest-path distance, and this repo computes every
other indicator as SQL against Postgres. Keeping the graph here rather than in an
in-process library is what lets that stay true: no graph artifact to cache on
disk, no cache key to get subtly wrong, no minutes of CPU held in a worker
process behind the GIL.

⚠ **Revision ranges are split across the fork** — see ``r036_run_plan``. This
track keeps r03x/r04x; the Hatchet track takes r050+. Never continue the other
branch's numbering.

⚠ ``pgrouting`` is **not** a trusted extension, so ``CREATE EXTENSION`` requires
superuser. That holds for the compose stack, for CI's service container, and for
a local Postgres.app install, which is every environment this project documents.
It will *not* hold on a managed instance that hands out a non-superuser role —
if the deployment target changes, this is the line that fails, at migrate time
rather than at query time, which is the right place for it to fail.

⚠ ``postgis/postgis`` does not ship pgRouting. ``infra/docker/docker-compose.yml``
and ``.github/workflows/ci.yml`` are pinned to ``pgrouting/pgrouting:16-3.4-3.6.1``
— the same Postgres major and PostGIS minor as the image it replaces, so this is
an addition rather than an upgrade.

**Why two tables, and why the ids are OSM's.** ``osmnx.graph_to_gdfs`` returns
edges already carrying ``u``/``v`` node ids and a ``length`` in metres — a graph
that is *already noded*. Storing those ids verbatim means:

- **``pgr_createTopology`` is never called.** It is the expensive step (an O(n)
  spatial dedup of every endpoint) and a trappy one: measured against pgRouting
  3.6.3, on an empty edge table it emits ``NOTICE: Unexpected error
  raise_exception``, **returns the string ``FAIL``, and does not raise** — so a
  build job that ignores the return value reports success while creating no
  vertices table at all. Not calling it removes that failure mode outright.
- ``road_node`` exists because routing starts from a *vertex*, so a block
  centroid has to be snapped to its nearest one. That is a KNN query against
  ``road_node``'s GIST index, which is why the table carries geometry at all.
- ``osm_id`` on the edge keeps lineage back to the OSM way for anyone who has to
  explain a specific distance.

``cost`` and ``reverse_cost`` are both edge length in metres, so the graph is
undirected — correct for the pedestrian network (``network_type="walk"``) these
indicators model, and wrong the moment anyone wants driving directions, at which
point ``reverse_cost`` becomes ``-1`` on one-ways. The columns are already shaped
for that; only the ingest changes.

**The network is ingest state, not an analysis feature.** These tables are
deliberately *not* rows in ``curated_geo.feature``: an alcaldía is on the order
of 10^5 segments, and materialising each as a feature would buy nothing the
``network_version`` attribute does not already give. Accessibility declares
``reads=("network_version",)``, so bumping that attribute is what invalidates the
indicators — and rebuilding these tables is what makes the bump meaningful.

Revision ID: r037
Revises: r036
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "r037"
down_revision = "r036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgrouting")

    # id is the OSM node id, not a surrogate: it is what the edge table's
    # source/target already reference, so minting our own would mean rewriting
    # every edge on ingest for no gain.
    op.execute("""
        CREATE TABLE curated_geo.road_node (
            id       bigint PRIMARY KEY,
            geometry geometry(Point, 4326) NOT NULL
        )
    """)
    # The nearest-vertex lookup for a block centroid. Without this the snap is a
    # sequential scan over every node in the city, once per block.
    op.execute(
        "CREATE INDEX road_node_geom_gist ON curated_geo.road_node USING GIST (geometry)"
    )

    op.execute("""
        CREATE TABLE curated_geo.road_edge (
            id           bigserial        PRIMARY KEY,
            osm_id       bigint,
            source       bigint           NOT NULL
                                          REFERENCES curated_geo.road_node(id)
                                          ON DELETE CASCADE,
            target       bigint           NOT NULL
                                          REFERENCES curated_geo.road_node(id)
                                          ON DELETE CASCADE,
            cost         double precision NOT NULL CHECK (cost >= 0),
            reverse_cost double precision NOT NULL CHECK (reverse_cost >= 0),
            geometry     geometry(LineString, 4326) NOT NULL
        )
    """)

    # pgr_drivingDistance drives its edge lookups off source/target.
    op.execute("CREATE INDEX road_edge_source_idx ON curated_geo.road_edge (source)")
    op.execute("CREATE INDEX road_edge_target_idx ON curated_geo.road_edge (target)")
    op.execute(
        "CREATE INDEX road_edge_geom_gist ON curated_geo.road_edge USING GIST (geometry)"
    )


def downgrade() -> None:
    # Edges first: they carry the foreign keys into road_node.
    op.execute("DROP TABLE IF EXISTS curated_geo.road_edge")
    op.execute("DROP TABLE IF EXISTS curated_geo.road_node")
    # The extension is deliberately left installed. Dropping it would break any
    # other topology in the database, and an unused extension costs nothing.
