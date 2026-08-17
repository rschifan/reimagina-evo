"""Distance from each block to the nearest amenity, two ways.

The collaborators' brief asks for both: accessibility from POIs, and then the
same thing with the road network in the loop, "where updating the road network
first affects accessibility downstream". They are separate indicator families
here because their costs differ by orders of magnitude, and that difference is
the whole argument for scheduling them apart.

``*_euclid_m``
    Straight-line metres via a geography KNN against the establishment layer.
    Index-backed, milliseconds. Optimistic by construction — it ignores rivers,
    motorways and walls — which makes it a useful lower bound on, and a cheap
    sanity check against, the network figure.

``*_network_m``
    Metres along the OSM pedestrian network, through pgRouting.

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

from dataclasses import dataclass

from reimagina.data.feature import get_type_id
from reimagina.indicators._hamilton_driver import build_indicator_driver
from reimagina.indicators.catalog import (
    ACCESSIBILITY_CUTOFF_M,
    ACCESSIBILITY_EUCLID,
    ACCESSIBILITY_NETWORK,
    ACCESSIBILITY_SPEC_CATEGORY,
    IndicatorSpec,
)
from reimagina.jobs._lifecycle import job_run
from reimagina.storage import backend_for_connection

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


@dataclass(frozen=True)
class ComputeResult:
    """Outcome of one accessibility compute."""

    job_run_id: str
    indicator_slug: str
    features_considered: int
    rows_written: int
    aoi_id: str | None


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

    ⚠ The **block's** snap distance is added to the route; the **amenity's** is
    not. ``pgr_drivingDistance`` takes no per-source initial cost, so an
    amenity's own walk-to-street cannot be expressed in the multi-source call.
    In practice POIs sit on streets and blocks may not, so this covers the case
    that actually distorts results — but it is an asymmetry, not an oversight.

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
            SELECT DISTINCT n.id
            FROM amenities a
            CROSS JOIN LATERAL (
                SELECT rn.id FROM curated_geo.road_node rn
                ORDER BY rn.geometry <-> a.geometry
                LIMIT 1
            ) n
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
            SELECT dd.node, dd.agg_cost
            FROM pgr_drivingDistance(
                %(edges_sql)s,
                (SELECT array_agg(id) FROM amenity_nodes),
                %(cutoff)s,
                directed := false,
                equicost := true
            ) dd
        )
        -- The walk to the network counts. Without `snap_m` a block kilometres
        -- from any road snaps to the nearest node anyway and reports that
        -- node's distance, hiding the entire journey to the street — a
        -- confident, wrong, and perfectly plausible-looking number. Adding it
        -- makes an off-network block exhaust the budget on its own and fall
        -- through to the cutoff, with no extra threshold constant to justify.
        SELECT bn.feature_id, bn.snap_m + r.agg_cost
        FROM block_nodes bn
        LEFT JOIN reach r ON r.node = bn.node_id
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


def _compute(
    conn,
    *,
    spec: IndicatorSpec,
    aoi_id: str | None = None,
    feature_ids: list[str] | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> ComputeResult:
    """Read, compute and write one accessibility indicator."""
    backend = backend_for_connection(conn)
    if backend.indicator_catalog_repo().get_indicator(spec.slug) is None:
        raise RuntimeError(
            f"Indicator {spec.slug!r} not found in indicator_catalog. "
            "Run `indicators seed-catalog` first."
        )
    block_type_id = get_type_id(conn, "MEX", "block")
    categories = ACCESSIBILITY_SPEC_CATEGORY[spec.slug]
    reader = _network_rows if spec.run_group == "network_access" else _euclid_rows

    with job_run(
        backend,
        model_family=spec.model_family or "accessibility",
        code_version=code_version,
        scenario_id=scenario_id,
    ) as job_run_id:
        rows = reader(
            conn,
            type_id=block_type_id,
            aoi_id=aoi_id,
            feature_ids=feature_ids,
            categories=categories,
        )
        frame = build_indicator_driver().execute(
            ["accessibility_result_frame"], inputs={"accessibility_rows": rows}
        )["accessibility_result_frame"]
        rows_written = backend.indicator_result_repo().bulk_write_numeric_results(
            list(frame.itertuples(index=False, name=None)),
            indicator_slug=spec.slug,
            job_run_id=job_run_id,
            scenario_id=scenario_id,
        )

    return ComputeResult(
        job_run_id=job_run_id,
        indicator_slug=spec.slug,
        features_considered=len(rows),
        rows_written=rows_written,
        aoi_id=aoi_id,
    )


def compute_for(spec: IndicatorSpec):
    """The compute fn for one accessibility spec, bound for the runner registry.

    Six near-identical closures rather than six hand-written functions: they
    differ only by spec, and writing them out would be six places to update
    when the signature moves.
    """

    def run(conn, **kwargs) -> ComputeResult:
        return _compute(conn, spec=spec, **kwargs)

    run.__name__ = f"compute_{spec.slug}"
    run.__doc__ = spec.description
    return run


#: slug -> compute fn, for every accessibility indicator.
ACCESSIBILITY_COMPUTE = {
    spec.slug: compute_for(spec)
    for spec in (*ACCESSIBILITY_EUCLID, *ACCESSIBILITY_NETWORK)
}
