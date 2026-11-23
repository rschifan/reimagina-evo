"""A block's baseline storey count, from the Overture buildings standing in it.

A scenario rule overrides storeys; without a baseline for them the scenario
workspace has nothing to compare a rule against. No INEGI layer carries
storeys, but Overture tags some buildings with ``num_floors``. A block's value
is the **footprint-weighted mean** storeys of every building on it: each
building weighs by its footprint clipped to the block, and counts its
``num_floors`` where tagged and 1 where not (never below 1). So
``storeys × footprint`` is the block's built floor area, the quantity
``scripts/seed_demo_attributes.py --fit-to-census`` divides by the census
dwellings to get m² per dwelling.

Why the mean over every building and not the median of the tagged ones: one
tagged three-storey shop among fifty untagged houses made the whole block
three storeys, and tripled its floor area. Untagged buildings count as one
storey because that is what nearly all of them are (Mexicali's tagged
buildings: 94% one storey), and the mean is what keeps floor area exact.

Written as a ``floors`` observation that keeps the buildings' own source, only
on blocks with at least one tagged building: every other block's mean is 1 by
definition, which is the fit's default. The fit writes the same value as
``num_levels`` (the dwelling formula's storeys), so planned floors and housing
read one height.

Weights are summed, not unioned: two overlapping footprints both weigh,
so the overlap counts twice. Only their mix of storeys is affected — a mean
over buildings that agree is the same either way — and the block's floor area
the fit uses is its *unioned* coverage times this mean, so no area is
double-counted there.

Rounded to 0.01 storey: a sum of floating-point areas can differ in its last
bits with the plan's summation order, and two installs must store the same
value.

⚠ Coverage is whatever Overture tags. Over Mexicali that is 1,117 of 343,707
buildings and 83 of 19,058 blocks (measured 2026-11-19).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from reimagina.assets.publish import asset_key_for_feature_type
from reimagina.data import catalog as cat
from reimagina.data import feature as feat
from reimagina.data import observation as obs

#: Planned floors' input; the fit copies it into ``num_levels``.
STOREY_KEY = "floors"

#: Floors of one building: its tag where it has one, else one storey.
_FLOORS = (
    "GREATEST(1, CASE WHEN jsonb_typeof(b.attributes->'num_floors') = 'number' "
    "THEN (b.attributes->>'num_floors')::double precision ELSE 1 END)"
)

#: A building ``b``'s footprint clipped to its block ``blk``. Most buildings
#: sit wholly inside one block, which skips the intersection. The census fit
#: unions these into lot coverage (overlapping footprints count once).
CLIPPED_FOOTPRINT = (
    "CASE WHEN ST_Within(b.geometry, blk.geometry) THEN b.geometry "
    "ELSE ST_Intersection(b.geometry, blk.geometry) END"
)

#: Its area, geodesic like every block area here: the storeys' weight.
CLIPPED_AREA = f"ST_Area(({CLIPPED_FOOTPRINT})::geography)"

#: Per block, from each (block, building) pair's clipped area, storeys and
#: tag, computed once per pair in the lateral (``OFFSET 0`` keeps the planner
#: from inlining it and repeating the intersection in every aggregate).
_MEANS = f"""
    SELECT min(b.source_id::text) FILTER (WHERE p.tagged),
           blk.id::text,
           ROUND((sum(p.area * p.floors) / NULLIF(sum(p.area), 0))::numeric, 2),
           count(*) FILTER (WHERE p.tagged)
    FROM curated_geo.feature blk
    JOIN curated_geo.feature b
      ON b.type_slug = 'building'
     AND ST_Intersects(blk.geometry, b.geometry)
    CROSS JOIN LATERAL (
        SELECT {CLIPPED_AREA} AS area,
               {_FLOORS} AS floors,
               jsonb_typeof(b.attributes->'num_floors') = 'number' AS tagged
        OFFSET 0
    ) p
    WHERE blk.type_slug = 'block'
      {{scope}}
    GROUP BY blk.id
    HAVING bool_or(p.tagged) AND sum(p.area) > 0
"""


def bootstrap(
    conn: Any,
    *,
    aoi_id: str | None = None,
    feature_ids: list[str] | None = None,
    code_version: str = "dev",
) -> dict[str, Any]:
    """Write ``floors`` for every block with a tagged building.

    Scoped to one AOI's blocks, to explicit block ids, or to every block.
    One ingestion run per buildings source, since an observation's source and
    run must agree. The caller commits.
    """
    params: list[Any]
    if feature_ids is not None:
        scope, params = "AND blk.id = ANY(%s::uuid[])", [feature_ids]
    elif aoi_id is not None:
        scope = (
            "AND blk.id IN (SELECT feature_id FROM curated_geo.aoi_feature "
            "WHERE aoi_id = %s::uuid)"
        )
        params = [aoi_id]
    else:
        scope, params = "", []
    with conn.cursor() as cur:
        cur.execute(_MEANS.format(scope=scope), params)
        rows = cur.fetchall()

    by_source: dict[str, list[dict[str, Any]]] = {}
    for source_id, block_id, storeys, buildings in rows:
        lineage = {
            "from": "global:building.num_floors",
            "statistic": "footprint_weighted_mean",
            "tagged_buildings": buildings,
        }
        by_source.setdefault(source_id, []).append(
            {
                "feature_id": block_id,
                "attribute_key": STOREY_KEY,
                "value_type": "numeric",
                "value": float(storeys),
                "lineage_ref": lineage,
            }
        )

    written = 0
    # Derived from the type, as the vector loader does, not a hard-coded key.
    block_key = (
        asset_key_for_feature_type(conn, feat.get_type_id(conn, "MEX", "block"))
        if by_source
        else None
    )
    for source_id, records in by_source.items():
        run_id = cat.start_ingestion_run(
            conn,
            source_id=source_id,
            orchestrator="manual",
            orchestrator_run_id=str(uuid4()),
            transformation_step="derive_block_storeys",
            code_version=code_version,
        )
        n = obs.bulk_upsert_observations(
            conn, records, source_id=source_id, ingestion_run_id=run_id
        )
        cat.complete_ingestion_run(
            conn,
            run_id,
            status="succeeded",
            rows_written=n,
            details={"blocks": len(records)},
            # Planned floors reads `floors` off mex.block; without a publish
            # it stays "fresh" against storeys that just changed.
            asset_key=block_key,
        )
        written += n
    return {
        "blocks_written": len({row[1] for row in rows}),
        "observations_written": written,
    }
