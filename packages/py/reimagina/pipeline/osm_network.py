"""Fetch the OSM walking network and load it into the pgRouting tables.

Accessibility needs a routable graph. OSM is the source, and ``osmnx`` is how it
is fetched: one ``graph_from_polygon`` call returns the network for an arbitrary
polygon **already noded** — every edge carrying ``u``/``v`` node ids and a
``length`` in metres. Those ids are stored verbatim, which is what lets the
loader skip ``pgr_createTopology`` entirely (see ``r037``: it is the expensive
step, and it fails *soft* on an empty table, returning the string ``FAIL``
without raising).

**Why osmnx and not the Overture pipeline.** ``pipeline/overture.py`` already
generalises over themes, so ``theme=transportation`` would need no new fetch
code — but it would need considerably more work downstream. Overture segments
arrive unnoded, so the topology would have to be built (the step above), and
selecting the *pedestrian* subset means mapping Overture's subtype/class
taxonomy onto what ``network_type="walk"`` already expresses. Overture
transportation is also a far heavier extract than a bounded Overpass query. The
cost of the alternative is two packages, ``osmnx`` and ``networkx``; everything
else it needs is already a dependency.

⚠ **Overpass is slow and rate-limited.** A 1.5 km² slice of Tlalpan measured
~125 s wall-clock — latency and server-side queueing, not local compute. That is
why fetching lives in ingest and never in an indicator's compute path: the graph
is fetched once, stored, and re-read by every subsequent run. The public
endpoint is a shared resource; do not loop over small tiles when one polygon
would do.

⚠ **Fetching and loading are separate functions on purpose.** ``load_graph``
takes frames and touches no network, so the database half is testable in CI
without an Overpass round-trip (and without a flaky external dependency in the
suite). Only ``fetch_walk_graph`` reaches out.

⚠ **Loading replaces the whole network.** ``road_node``/``road_edge`` hold one
graph, not one per AOI — sized for the prototype's single pilot area. Loading a
second AOI discards the first. Scoping the tables by AOI is the upgrade path;
until something needs two cities live at once, a column nobody filters on would
be dead weight.

⚠ **Nothing here invalidates anything downstream yet.** This function opens no
``ingestion_run`` and publishes no version, so reloading the graph leaves every
accessibility indicator reporting a value computed against the *previous*
network. The instruction that used to stand here — "after loading, bump the
``network_version`` attribute" — described a step no code performs: the only
writer of that attribute anywhere is ``scripts/seed_demo_attributes.py``, which
sets it to a constant.

The replacement is registered but not yet wired: ``global.road_network`` is an
``external`` asset with an ``observed`` version strategy
(``reimagina.assets.registry``), so once observation runs, reloading the graph
moves its version and the three ``*_network_m`` indicators go stale on their
own. Until then this remains a manual step, and the honest statement is that it
is not performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg2
import psycopg2.extras

#: The pedestrian graph — what accessibility should route over, since it
#: includes footways and ignores car-only restrictions like one-ways. Passed
#: through to osmnx, which accepts other network types ("drive", "bike"); add a
#: constant when something actually asks for one.
WALK = "walk"


@dataclass(frozen=True)
class LoadResult:
    """Outcome of a network load."""

    nodes_written: int
    edges_written: int
    network_type: str
    total_length_m: float


def _osm_id(value: object) -> int | None:
    """One OSM way id for lineage, from osmnx's mixed-type ``osmid`` column.

    ⚠ ``simplify=True`` collapses a run of ways into a single edge, and that
    edge's ``osmid`` is then a *list* rather than an int — the column holds both
    types at once, which is also why the frame cannot be written to parquet
    unmodified. Taking the first id keeps a usable pointer back to OSM; storing
    the whole list would mean an array column that nothing queries.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        return int(first) if isinstance(first, int) else None
    return None


def fetch_walk_graph(polygon, *, network_type: str = WALK):
    """Fetch an OSM graph for ``polygon`` and return ``(nodes, edges)`` frames.

    ``polygon`` is a shapely geometry in EPSG:4326. Returns the two GeoDataFrames
    ``osmnx.graph_to_gdfs`` produces, with ``edges`` reset so ``u``/``v``/``key``
    are ordinary columns rather than a MultiIndex.

    ⚠ Imported lazily. ``osmnx`` pulls in ``networkx`` and does non-trivial work
    at import; every other module in this package is importable without it, and
    a top-level import would put that cost on anything that touches
    ``reimagina.pipeline`` — including the API.
    """
    import osmnx as ox

    graph = ox.graph_from_polygon(polygon, network_type=network_type, simplify=True)
    nodes, edges = ox.graph_to_gdfs(graph)
    return nodes, edges.reset_index()


def aoi_polygon(conn, aoi_id: str):
    """The AOI's geometry as a shapely polygon in EPSG:4326."""
    from shapely import wkb

    with conn.cursor() as cur:
        cur.execute(
            "SELECT geometry FROM curated_geo.area_of_interest WHERE id = %s::uuid",
            (aoi_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no area_of_interest with id {aoi_id!r}")
    return wkb.loads(bytes.fromhex(row[0]))


def load_graph(conn, nodes, edges, *, network_type: str = WALK) -> LoadResult:
    """Replace the stored network with ``nodes``/``edges``.

    ``nodes`` is indexed by OSM node id with ``x``/``y`` columns; ``edges`` has
    ``u``, ``v``, ``length`` and a LineString ``geometry`` — i.e. exactly what
    ``osmnx.graph_to_gdfs`` returns.

    Both costs are set to ``length``, making the graph undirected. Correct for a
    pedestrian network; a driving network would set ``reverse_cost`` to -1 on
    one-ways instead.

    ⚠ Nodes are inserted before edges, because the edges' foreign keys point at
    them. ``TRUNCATE ... CASCADE`` on road_node clears both tables, which is why
    the edge table is not truncated separately.
    """
    node_rows = [
        (int(osm_id), float(row.x), float(row.y)) for osm_id, row in nodes.iterrows()
    ]
    known = {osm_id for osm_id, _x, _y in node_rows}

    edge_rows = []
    total_length = 0.0
    for row in edges.itertuples(index=False):
        source, target = int(row.u), int(row.v)
        # osmnx keeps the graph internally consistent, so this should never fire
        # — but a dangling endpoint would surface as a foreign-key violation
        # mid-COPY, aborting the whole load with a message that points at the
        # database rather than at the fetch. Cheaper to say so plainly.
        if source not in known or target not in known:
            raise ValueError(
                f"edge {source}->{target} references a node absent from the "
                "node frame; fetch and load must come from the same graph"
            )
        length = float(row.length)
        total_length += length
        edge_rows.append(
            (
                _osm_id(getattr(row, "osmid", None)),
                source,
                target,
                length,
                length,
                # Raw WKB bytes, which psycopg2 adapts to bytea. NOT wkb_hex:
                # a hex *string* cast to bytea gives Postgres the ASCII of the
                # hex, and ST_GeomFromWKB fails with "Invalid endian flag".
                # Mirrors bulk_insert_features_from_parquet's template.
                psycopg2.Binary(row.geometry.wkb),
            )
        )

    with conn.cursor() as cur:
        # CASCADE reaches road_edge through its foreign keys, so one statement
        # clears both tables in the right order.
        cur.execute("TRUNCATE curated_geo.road_node CASCADE")
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO curated_geo.road_node (id, geometry) VALUES %s",
            node_rows,
            template="(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))",
            page_size=1000,
        )
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO curated_geo.road_edge "
            "(osm_id, source, target, cost, reverse_cost, geometry) VALUES %s",
            edge_rows,
            template="(%s, %s, %s, %s, %s, ST_SetSRID(ST_GeomFromWKB(%s), 4326))",
            page_size=1000,
        )

    return LoadResult(
        nodes_written=len(node_rows),
        edges_written=len(edge_rows),
        network_type=network_type,
        total_length_m=total_length,
    )


def bootstrap_network(conn, *, aoi_id: str, network_type: str = WALK) -> dict[str, Any]:
    """Fetch the AOI's OSM network and load it. Minutes, not seconds."""
    polygon = aoi_polygon(conn, aoi_id)
    nodes, edges = fetch_walk_graph(polygon, network_type=network_type)
    result = load_graph(conn, nodes, edges, network_type=network_type)
    conn.commit()
    return {
        "aoi_id": aoi_id,
        "network_type": result.network_type,
        "nodes": result.nodes_written,
        "edges": result.edges_written,
        "total_length_km": round(result.total_length_m / 1000, 1),
    }
