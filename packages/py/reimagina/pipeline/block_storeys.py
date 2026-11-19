"""A block's baseline storey count, from the Overture buildings standing in it.

A scenario rule overrides ``floors`` (planned floors); without a baseline for
it the scenario workspace has nothing to compare a rule against. No INEGI
layer carries storeys, but Overture tags some buildings with ``num_floors``. A
block's value is the median over the buildings whose point-on-surface lies in
it, rounded to a whole storey and never below one, written as a ``floors``
observation that keeps the buildings' own source.

⚠ ``floors`` only, not ``num_levels``. The dwelling formula's storeys live in
the fallback layer, where ``scripts/seed_demo_attributes.py --fit-to-census``
sets them to these same storeys and fits m² per dwelling so a block's
dwellings equal the census. Written here as an observation, a real height
would shadow that layer's 0 on a block the census found without dwellings and
build homes on it: 6,215 of them over 37 Mexicali blocks, measured.

⚠ Coverage is whatever Overture tags. Over Mexicali that is 1,117 of 343,707
buildings and 83 of 19,058 blocks (measured 2026-11-19). A block with no tagged
building gets no observation and keeps its fallback: ``effective_numeric``
resolves override, then observation, then the base jsonb that
``scripts/seed_demo_attributes.py`` writes.

⚠ Point-on-surface, not overlap. A building straddling two blocks counts once,
in the block holding the point ``feature.geometry_centroid`` already stores.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from reimagina.data import catalog as cat
from reimagina.data import observation as obs

#: Planned floors' input; see the module docstring for why not ``num_levels``.
STOREY_KEY = "floors"

_MEDIANS = """
    SELECT b.source_id::text,
           blk.id::text,
           -- ::numeric: round() on double precision is half-to-even, so a
           -- 2.5 median would read 2; on numeric it rounds half up.
           GREATEST(1, ROUND((percentile_cont(0.5) WITHIN GROUP (
               ORDER BY (b.attributes->>'num_floors')::double precision
           ))::numeric))::int,
           count(*)
    FROM curated_geo.feature blk
    JOIN curated_geo.feature b
      ON b.type_slug = 'building'
     AND jsonb_typeof(b.attributes->'num_floors') = 'number'
     AND ST_Contains(blk.geometry, b.geometry_centroid)
    WHERE blk.type_slug = 'block'
      {scope}
    GROUP BY 1, 2
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
        cur.execute(_MEDIANS.format(scope=scope), params)
        rows = cur.fetchall()

    by_source: dict[str, list[dict[str, Any]]] = {}
    for source_id, block_id, storeys, buildings in rows:
        lineage = {
            "from": "global:building.num_floors",
            "statistic": "median",
            "buildings": buildings,
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
        )
        written += n
    return {
        "blocks_written": len({row[1] for row in rows}),
        "observations_written": written,
    }
