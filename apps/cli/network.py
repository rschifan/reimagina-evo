"""CLI helpers for the routing network: fetch from OSM, load, publish.

``reimagina.pipeline.osm_network.bootstrap_network`` has done the whole job
for a while — fetch an AOI's pedestrian graph with osmnx, replace
``curated_geo.road_node``/``road_edge``, and publish the edges as
``global:road`` features with the ODbL provenance the feature table forces
into existence. It had no command, so the only ways to populate a real
street network were `scripts/synthetic_territory.py` (which generates a fake
one) and writing Python by hand.

That absence was load-bearing in a bad way. The accessibility family's
own guard names the function in its error text:

    needs="curated_geo.road_edge — the graph to route over",
    loader="`reimagina.pipeline.osm_network.bootstrap_network`",

— so a user who ran `indicators compute --slug
accessibility_block_school_network_m` on real data was told to go call a
Python function. And `AGENTS.md` documents the street layer as the map's
only spatial context, with no command to fill it.
"""

from __future__ import annotations

from typing import Any

from reimagina.db import connect
from reimagina.pipeline.osm_network import WALK, bootstrap_network


def bootstrap_osm_network(
    *,
    dsn: str,
    aoi_id: str,
    network_type: str = WALK,
    publish_features: bool = True,
    clip_bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Fetch, load and publish the AOI's OSM network.

    ⚠ **Replaces the whole network, not just this AOI's part of it.**
    ``load_graph`` truncates ``road_node CASCADE``, which is the loader's
    documented behaviour ("loading replaces the whole network"). Running
    this for a second AOI discards the first one's graph rather than adding
    to it. Said here because the ``--aoi-id`` flag reads like a scope and is
    in fact only the fetch boundary.
    """
    with connect(dsn) as conn:
        summary = bootstrap_network(
            conn,
            aoi_id=aoi_id,
            network_type=network_type,
            publish_features=publish_features,
            clip_bbox=clip_bbox,
        )
    return {"dsn": dsn, "status": "ok", **summary}
