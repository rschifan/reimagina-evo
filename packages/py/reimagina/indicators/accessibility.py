"""Distance from each block to the nearest amenity, two ways.

The collaborators' brief asks for both: accessibility from POIs, and then the
same thing with the road network in the loop, "where updating the road network
first affects accessibility downstream". They are separate indicator families
here because their costs differ materially, and that difference is the whole
argument for scheduling them apart.

``*_euclid_m``
    Straight-line metres via a geography KNN against the establishment layer.
    Optimistic by construction — it ignores rivers, motorways and walls — which
    makes it a useful lower bound on, and a sanity check against, the network
    figure.

``*_network_m``
    Metres along the OSM pedestrian network, through pgRouting.

⚠ **The straight-line family is the expensive one**, which is the reverse of
what these names suggest and of what this module claimed before it was
measured. The KNN runs once per block; the Dijkstra runs once per *category*
and amortizes over every block. So euclid is linear in block count and network
is flat, and the gap widens with scale — 1.5x at 1,500 Tlalpan blocks, 2.4x at
5,273. See ``ACCESSIBILITY_CATEGORIES``' neighbouring comment in ``catalog`` for
the numbers, and ``scripts/bench_real_data.py`` to re-measure.

**The algorithm that makes the network family affordable.** The obvious shape —
one ``pgr_drivingDistance`` per block — is one Dijkstra per block, which on a
city is absurd. Instead this runs **one multi-source Dijkstra per category**:
every amenity of that category is a start vertex, ``equicost := true`` assigns
each reached node to its *nearest* source, and blocks then join to that result.
Cost goes from O(blocks) shortest-path runs to O(1) per category. Verified
against pgRouting 3.6.3: with ``equicost`` each node appears exactly once,
without it once per source.

⚠ **``pgr_createTopology`` is never called.** ``road_node``/``road_edge`` carry
OSM's own node ids from the ingest (``pipeline/osm_network``), so the graph
arrives already noded. See ``r037`` for why that matters — the function fails
*soft* on an empty table, returning ``FAIL`` without raising.

⚠ **An empty road network raises rather than reporting 1500 everywhere.** A
block that cannot reach an amenity is a finding; a database with no roads in it
is a misconfiguration, and reporting the two identically would hide the second
behind a plausible-looking choropleth.
"""

from __future__ import annotations


from reimagina.data.feature import get_type_id
from reimagina.indicators._compute import (
    ComputeResult as ComputeResult,
    compute_indicator,
)
from reimagina.indicators.catalog import (
    ACCESSIBILITY_CUTOFF_M,
    ACCESSIBILITY_EUCLID,
    ACCESSIBILITY_NETWORK,
    ACCESSIBILITY_SPEC_CATEGORY,
    IndicatorSpec,
)

#: The edge set pgRouting walks. A string because pgr_* takes its graph as SQL.
_EDGES_SQL = "SELECT id, source, target, cost, reverse_cost FROM curated_geo.road_edge"

# Blocks in scope, with the centroid the distance is measured from.
#
# `geometry_centroid` is the generated ST_PointOnSurface column from r027 — a
# point guaranteed to lie *inside* the polygon, unlike ST_Centroid, which can
# fall outside a U-shaped block and snap to the wrong side of a road.
_BLOCKS_CTE = """
    blocks AS (
        SELECT f.id::text AS feature_id, f.geometry_centroid AS centroid
        FROM curated_geo.feature f
        {aoi_join}
        WHERE f.type_id = %(type_id)s {feature_filter}
    )
"""

_AOI_JOIN = """
        JOIN curated_geo.area_of_interest aoi
          ON aoi.id = %(aoi_id)s::uuid AND ST_Intersects(f.geometry, aoi.geometry)
"""

# Amenities of one category. `attributes->>'category'` rather than `name`:
# points.py writes the source category to both, but attributes is the
# structured one and name is a display label that a later ingest may localise.
_AMENITIES_CTE = """
    amenities AS (
        SELECT f.geometry
        FROM curated_geo.feature f
        WHERE f.type_slug = 'establishment'
          AND f.attributes->>'category' = ANY(%(categories)s)
    )
"""


def _scope(aoi_id: str | None, feature_ids: list[str] | None) -> tuple[str, str]:
    """The AOI join and feature filter fragments for the blocks CTE."""
    return (
        _AOI_JOIN if aoi_id else "",
        "AND f.id = ANY(%(feature_ids)s::uuid[])" if feature_ids else "",
    )


def _euclid_rows(conn, *, type_id, aoi_id, feature_ids, categories):
    """``(feature_id, straight_line_m | None)`` per block.

    ``ST_DWithin`` on geography bounds the search so the KNN cannot wander the
    whole city for a block with no amenity nearby; the LATERAL then takes the
    single nearest. Ordering by ``geography <->`` gives true metres rather than
    the degrees a planar ``<->`` on EPSG:4326 would compare.
    """
    aoi_join, feature_filter = _scope(aoi_id, feature_ids)
    sql = f"""
        WITH {_BLOCKS_CTE.format(aoi_join=aoi_join, feature_filter=feature_filter)},
        {_AMENITIES_CTE}
        SELECT b.feature_id, nearest.distance_m
        FROM blocks b
        LEFT JOIN LATERAL (
            SELECT ST_Distance(b.centroid::geography, a.geometry::geography)
                   AS distance_m
            FROM amenities a
            WHERE ST_DWithin(b.centroid::geography, a.geometry::geography, %(cutoff)s)
            ORDER BY b.centroid::geography <-> a.geometry::geography
            LIMIT 1
        ) nearest ON true
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "type_id": type_id,
                "aoi_id": aoi_id,
                "feature_ids": feature_ids,
                "categories": list(categories),
                "cutoff": ACCESSIBILITY_CUTOFF_M,
            },
        )
        return cur.fetchall()


def _network_rows(conn, *, type_id, aoi_id, feature_ids, categories):
    """``(feature_id, network_m | None)`` per block, one Dijkstra per category.

    Amenities and block centroids are both snapped to their nearest graph
    vertex, then a single multi-source ``pgr_drivingDistance`` from every
    amenity vertex labels each reachable node with its distance to the closest
    one. Blocks read their answer off that.

    ⚠ Two blocks snapping to the same vertex get the same *network* leg. Their
    reported distances still differ by their own snap distances, but the method's
    resolution is bounded by the graph's node spacing. A finer answer needs the
    amenity's true position along an edge, which is what ``pgr_withPoints`` is
    for and is more machinery than a prototype needs.

    ⚠ **Both** snap distances are added — the block's and the amenity's.
    ``pgr_drivingDistance`` takes no per-source *initial cost*, but it returns
    ``start_vid``, so the amenity's own walk to the street is recovered by
    joining that back to the vertex it came from. Without it the amenity
    teleports onto the network for free and the network distance can come out
    shorter than the straight line — measured at 652 of 15,819 block-category
    pairs on real Tlalpan data.

    ⚠ Remaining approximation: ``equicost`` picks the nearest *vertex*, not the
    nearest amenity once its snap is counted. An amenity 700 m off a close
    vertex can therefore win over one 10 m off a slightly farther vertex. Real
    snaps are small — median 28.5 m, p95 74.2 m over 1,785 Tlalpan amenities —
    so this is second-order, and closing it needs ``pgr_withPoints``.

    **No amenities of this category needs no special case.** ``array_agg`` over
    an empty set is NULL, and ``pgr_drivingDistance`` given a NULL start array
    returns zero rows rather than raising (measured on pgRouting 3.6.3). So
    ``reach`` comes back empty, the LEFT JOIN yields a NULL ``agg_cost``, and
    ``bounded_distance`` reports the cutoff — which is the wanted answer. This
    function previously carried a short circuit and an ``EXISTS`` guard for a
    failure that does not occur; ``test_no_amenity_of_that_category_reports_the_cutoff``
    covers the path that replaced them.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM curated_geo.road_edge")
        if cur.fetchone()[0] == 0:
            raise RuntimeError(
                "curated_geo.road_edge is empty — no network to route over. "
                "Load one with reimagina.pipeline.osm_network.bootstrap_network "
                "before computing network accessibility."
            )

    aoi_join, feature_filter = _scope(aoi_id, feature_ids)
    sql = f"""
        WITH {_BLOCKS_CTE.format(aoi_join=aoi_join, feature_filter=feature_filter)},
        {_AMENITIES_CTE},
        amenity_nodes AS (
            -- min(): several amenities can snap to one vertex, and the nearest
            -- of them is the one a walker actually reaches from it.
            SELECT n.id, min(n.snap_m) AS snap_m
            FROM amenities a
            CROSS JOIN LATERAL (
                SELECT rn.id,
                       ST_Distance(a.geometry::geography, rn.geometry::geography)
                           AS snap_m
                FROM curated_geo.road_node rn
                ORDER BY rn.geometry <-> a.geometry
                LIMIT 1
            ) n
            GROUP BY n.id
        ),
        block_nodes AS (
            SELECT b.feature_id, n.id AS node_id, n.snap_m
            FROM blocks b
            CROSS JOIN LATERAL (
                SELECT rn.id,
                       ST_Distance(b.centroid::geography, rn.geometry::geography)
                           AS snap_m
                FROM curated_geo.road_node rn
                ORDER BY rn.geometry <-> b.centroid
                LIMIT 1
            ) n
        ),
        reach AS (
            -- One multi-source Dijkstra. equicost := true keeps each node once,
            -- labelled with the distance to its NEAREST amenity vertex.
            -- start_vid names that vertex, which is how the amenity's own walk
            -- to the street is recovered below.
            SELECT dd.node, dd.start_vid, dd.agg_cost
            FROM pgr_drivingDistance(
                %(edges_sql)s,
                (SELECT array_agg(id) FROM amenity_nodes),
                %(cutoff)s,
                directed := false,
                equicost := true
            ) dd
        )
        -- Both walks to the network count: the block's, and the amenity's.
        --
        -- Without the block's, a block kilometres from any road snaps to the
        -- nearest node anyway and reports that node's distance, hiding the
        -- entire journey to the street.
        --
        -- Without the amenity's, the route teleports it onto its nearest vertex
        -- for free — which made the network distance come out *shorter* than
        -- the straight line for 652 of 15,819 block-category pairs on real
        -- Tlalpan data, where amenities sit up to 699 m off-network. Both are
        -- the same confident, plausible-looking error.
        --
        -- Either walk alone can exhaust the budget, so a genuinely remote block
        -- or amenity falls through to the cutoff with no threshold constant to
        -- justify.
        SELECT bn.feature_id, bn.snap_m + r.agg_cost + an.snap_m
        FROM block_nodes bn
        LEFT JOIN reach r ON r.node = bn.node_id
        LEFT JOIN amenity_nodes an ON an.id = r.start_vid
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "type_id": type_id,
                "aoi_id": aoi_id,
                "feature_ids": feature_ids,
                "categories": list(categories),
                "cutoff": ACCESSIBILITY_CUTOFF_M,
                "edges_sql": _EDGES_SQL,
            },
        )
        return cur.fetchall()


def _reader_for(spec: IndicatorSpec, rows_fn):
    """Bind one spec's categories and row source into the shared read shape.

    Which family a spec belongs to is known where the specs are built and
    nowhere else, so `rows_fn` is passed in rather than derived here.
    """
    categories = ACCESSIBILITY_SPEC_CATEGORY[spec.slug]

    def read(conn, *, aoi_id, feature_ids, scenario_id):
        return rows_fn(
            conn,
            type_id=get_type_id(conn, "MEX", "block"),
            aoi_id=aoi_id,
            feature_ids=feature_ids,
            categories=categories,
        )

    return read


def _bind(spec: IndicatorSpec, rows_fn):
    return compute_indicator(
        spec,
        reader=_reader_for(spec, rows_fn),
        node="accessibility_result_frame",
        input_key="accessibility_rows",
        default_family="accessibility",
    )


#: slug -> compute fn, for every accessibility indicator.
ACCESSIBILITY_COMPUTE = {
    **{spec.slug: _bind(spec, _euclid_rows) for spec in ACCESSIBILITY_EUCLID},
    **{spec.slug: _bind(spec, _network_rows) for spec in ACCESSIBILITY_NETWORK},
}
