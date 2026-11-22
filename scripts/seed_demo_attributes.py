#!/usr/bin/env python
"""Seed the fixture attributes the edificability and accessibility demos read.

# ponytail: fixture data. Delete this script when a real cadastre and a real
# zoning table land — everything here exists because nothing in the shipped
# pipeline populates a scenario-editable attribute today.

That gap is worth stating plainly, because it is not obvious: INEGI's marco
geoestadístico carries only identifiers (`CVE_ENT`, `CVE_MUN`, `CVE_AGEB`, …),
Overture's `num_floors` lands on `global:building` rather than on blocks, and
`infra/db/seeds/` is empty. Two real sources cover part of it —
`overture block-storeys` writes a block's median `num_floors` as a `floors`
observation (83 Mexicali blocks are tagged) and `inegi-censo bootstrap
--columns prom_ocup` writes household size — and nothing writes `num_levels`
or `unit_size`, so the edificability family returns zero rows against a
freshly bootstrapped database without this script.

Writes six keys onto `curated_geo.feature.attributes` for `MEX:block` features:

    num_levels            1–6, varying per block
    floors                same value as num_levels (see below)
    unit_size             60      m² per dwelling
    people_per_household  3.6     ~INEGI prom_ocup
    poi_version           1       bumped to invalidate POI accessibility
    network_version       1       bumped to invalidate network accessibility

⚠ **`floors` and `num_levels` are the same physical quantity under two names**,
and both are written with the same value rather than one being derived from the
other, because nothing in the schema ties them together. `num_levels` is the
edificability input; `floors` is what `urban_form_block_planned_floors` reads
and what a scenario overrides for an upzoning what-if. Seeding only `num_levels`
left planned_floors returning zero rows — exactly the emptiness this docstring
warns about, reproduced by the script written to prevent it.

Unifying them is the right end state and is deliberately *not* done here: the
two keys have different invalidation semantics in `impact.py` (editing
`num_levels` must recompute three edificability indicators; editing `floors`
must recompute one), and collapsing them is a catalog change with test
consequences, not a fixture change. Writing both keeps the fixture honest about
the duplication instead of hiding it.

⚠ **Deterministic, never `random`.** `num_levels` is derived from a blake2b of
the block's stable code — `source_record_ref`, the CVEGEO for INEGI blocks — so
re-running produces identical values and two installs see the same map. Not the
feature id: that is `gen_random_uuid()`, so hashing it gave every install
different storeys and different edificability numbers. An unseeded RNG here would make every run a different dataset and
every comparison meaningless — the same class of trap `impact.py` documents at
length for `PYTHONHASHSEED`.

⚠ **Merges rather than replaces** (`attributes || %s::jsonb`). The column already
holds the ingest's identifier keys; assigning over it would destroy the
`CVEGEO` lineage that joins blocks to census data.

⚠ **Bypasses `observation` provenance.** A real attribute belongs in
`curated_geo.observation`, which carries NOT NULL source and ingestion-run
foreign keys and a trigger asserting they agree. This writes the base jsonb
instead — correct for a fixture, wrong for real data. `effective_numeric`
resolves override → observation → jsonb, so real observations later shadow
these values without anyone having to delete them first.

**Fitted to the census (``--fit-to-census``).** The flat values above are
generic, and on a real city they are absurd: over Mexicali they gave 13.8
million potential dwellings against the census's 340,169, because the 1–6
storeys average 3.5 where the city is mostly single-storey, and the formula
builds every block over wall to wall at 60 m². Fitted mode keeps the values
synthetic but shapes them to the place, so the baseline reads as the city that
exists and a scenario rule's change is measured against it:

    num_levels / floors   drawn per block (keyed by its CVEGEO) from the storey mix of the
                          Overture buildings in the selected blocks
                          (Mexicali: 94% one storey); 1–6 if none is tagged
                          — or, where a ``floors`` observation holds a real
                          height, that height
    unit_size             block area × storeys ÷ census dwellings
                          (``censo_2020_vivtot``), so the formula gives the
                          census count exactly
    people_per_household  the median of the census's own values
                          (``people_per_household`` observations), else 3.6

⚠ A block the census found without dwellings gets ``num_levels`` 0, so the
baseline invents no homes on it, while ``floors`` keeps a height for planned
floors; its ``unit_size`` is the fitted median, which multiplies zero. This
holds only because real heights land as ``floors`` observations and never as
``num_levels`` ones (``reimagina.pipeline.block_storeys``): an observation
would shadow the 0.

Usage:

    python scripts/seed_demo_attributes.py --aoi-id <uuid>
    python scripts/seed_demo_attributes.py --aoi-id <uuid> --fit-to-census
    python scripts/seed_demo_attributes.py --dry-run          # all blocks, no write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import psycopg2
import psycopg2.extras  # execute_batch lives on the extras module, not the root

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost/reimagina")

#: Fixed for every block — only num_levels varies.
CONSTANT_ATTRIBUTES = {
    "unit_size": 60,
    "people_per_household": 3.6,
    "poi_version": 1,
    "network_version": 1,
}

MIN_LEVELS = 1
MAX_LEVELS = 6


def levels_for(block_code: str) -> int:
    """A stable 1–6 storey count for a block code (``source_record_ref``).

    blake2b rather than `hash()`: the builtin is salted per process by
    PYTHONHASHSEED, so it would hand out different values on every run — which
    is precisely the non-determinism this script exists to avoid.
    """
    digest = hashlib.blake2b(block_code.encode("utf-8"), digest_size=8).digest()
    return MIN_LEVELS + int.from_bytes(digest, "big") % (MAX_LEVELS - MIN_LEVELS + 1)


def storeys_from_mix(block_code: str, mix: dict[int, int]) -> int:
    """A stable storey count for a block, drawn from ``{storeys: buildings}``.

    The same blake2b as ``levels_for``, mapped onto the mix's cumulative
    counts, so the share of blocks at each height follows the share of
    buildings at it and a re-run draws the same value for the same block.
    """
    digest = hashlib.blake2b(block_code.encode("utf-8"), digest_size=8).digest()
    pick = int.from_bytes(digest, "big") % sum(mix.values())
    for storeys, count in sorted(mix.items()):
        if pick < count:
            return storeys
        pick -= count
    raise AssertionError("unreachable: pick < sum(mix)")


def _fitted_payloads(cur, feature_ids: list[str]) -> list[dict[str, object]]:
    """Per-block synthetic values shaped to the census; see the module docstring."""
    cur.execute(
        """
        SELECT ROUND((b.attributes->>'num_floors')::numeric)::int, count(*)
        FROM curated_geo.feature blk
        JOIN curated_geo.feature b
          ON b.type_slug = 'building'
         AND jsonb_typeof(b.attributes->'num_floors') = 'number'
         AND ST_Contains(blk.geometry, b.geometry_centroid)
        WHERE blk.id = ANY(%s::uuid[])
          AND (b.attributes->>'num_floors')::numeric >= 0.5
        GROUP BY 1
        """,
        (feature_ids,),
    )
    mix = dict(cur.fetchall())
    cur.execute(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY value_numeric)
        FROM curated_geo.observation
        WHERE attribute_key = 'people_per_household' AND is_current
          AND feature_id = ANY(%s::uuid[])
        """,
        (feature_ids,),
    )
    household = cur.fetchone()[0] or CONSTANT_ATTRIBUTES["people_per_household"]

    def current(key: str) -> str:
        return (
            "(SELECT o.value_numeric FROM curated_geo.observation o "
            f"WHERE o.feature_id = f.id AND o.attribute_key = '{key}' AND o.is_current)"
        )

    # Area as the indicator measures it (geodesic), or the fit is off by the
    # projection's scale and the census count is not reproduced.
    cur.execute(
        f"""
        SELECT f.id::text, f.source_record_ref, ST_Area(f.geometry::geography),
               {current("censo_2020_vivtot")}, {current("floors")}
        FROM curated_geo.feature f
        WHERE f.id = ANY(%s::uuid[])
        """,
        (feature_ids,),
    )
    return fit_payloads(cur.fetchall(), mix, household)


def fit_payloads(
    blocks: list[tuple[str, str, float, float | None, float | None]],
    mix: dict[int, int],
    household: float,
) -> list[dict[str, object]]:
    """Fitted values for ``(id, code, area, dwellings, real_floors)`` rows."""
    payloads: dict[str, dict[str, object]] = {}
    sizes: list[float] = []
    for fid, code, area, dwellings, real_floors in blocks:
        storeys = storeys_from_mix(code, mix) if mix else levels_for(code)
        levels = int(real_floors) if real_floors else storeys
        payload: dict[str, object] = {
            "poi_version": CONSTANT_ATTRIBUTES["poi_version"],
            "network_version": CONSTANT_ATTRIBUTES["network_version"],
            "people_per_household": round(float(household), 2),
            "floors": storeys,
        }
        if dwellings and dwellings > 0 and area > 0:
            size = area * levels / dwellings
            payload.update(num_levels=levels, unit_size=size)
            sizes.append(size)
        else:
            payload["num_levels"] = 0
        payloads[fid] = payload
    sizes.sort()
    median = sizes[len(sizes) // 2] if sizes else CONSTANT_ATTRIBUTES["unit_size"]
    for payload in payloads.values():
        payload.setdefault("unit_size", median)
    return [{"feature_id": fid, **p} for fid, p in payloads.items()]


def _block_ids(cur, aoi_id: str | None) -> list[tuple[str, str]]:
    """``(id, source_record_ref)`` of every MEX:block, optionally in one AOI.

    The AOI join mirrors `FeatureRepository.geodesic_metrics_by_type`, so the
    seeded set and the set the indicators later compute over are the same.
    """
    if aoi_id is None:
        cur.execute("""
            SELECT f.id::text, f.source_record_ref
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
            WHERE t.context = 'MEX' AND t.type_slug = 'block'
        """)
    else:
        cur.execute(
            """
            SELECT f.id::text, f.source_record_ref
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
            JOIN curated_geo.area_of_interest aoi
              ON aoi.id = %s::uuid AND ST_Intersects(f.geometry, aoi.geometry)
            WHERE t.context = 'MEX' AND t.type_slug = 'block'
            """,
            (aoi_id,),
        )
    return [(fid, ref) for fid, ref in cur.fetchall()]


def constant_payloads(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(json, id)`` for the unfitted seed of ``(id, code)`` blocks."""
    return [
        (
            json.dumps(
                {
                    **CONSTANT_ATTRIBUTES,
                    # Same value, two keys -- see the module docstring.
                    "num_levels": levels_for(code),
                    "floors": levels_for(code),
                }
            ),
            fid,
        )
        for fid, code in blocks
    ]


def seed(
    dsn: str, *, aoi_id: str | None, dry_run: bool, fit_to_census: bool = False
) -> dict[str, object]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            blocks = _block_ids(cur, aoi_id)
            feature_ids = [fid for fid, _ in blocks]
            if not feature_ids:
                return {
                    "blocks": 0,
                    "written": 0,
                    "note": (
                        "no MEX:block features found — ingest the INEGI marco "
                        "geoestadístico first, and check the --aoi-id"
                    ),
                }

            if fit_to_census:
                payloads = [
                    (
                        json.dumps({k: v for k, v in p.items() if k != "feature_id"}),
                        str(p["feature_id"]),
                    )
                    for p in _fitted_payloads(cur, feature_ids)
                ]
            else:
                payloads = constant_payloads(blocks)
            if dry_run:
                sample = json.loads(payloads[0][0])
                return {
                    "blocks": len(feature_ids),
                    "written": 0,
                    "dry_run": True,
                    "sample": sample,
                }

            # Merge, never assign: `||` keeps the ingest's CVEGEO lineage keys.
            psycopg2.extras.execute_batch(
                cur,
                "UPDATE curated_geo.feature "
                "SET attributes = attributes || %s::jsonb, updated_at = now() "
                "WHERE id = %s::uuid",
                payloads,
                page_size=500,
            )
        conn.commit()
        return {"blocks": len(feature_ids), "written": len(feature_ids)}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument(
        "--aoi-id", help="Restrict to blocks intersecting this AOI (default: all)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without writing it",
    )
    parser.add_argument(
        "--fit-to-census",
        action="store_true",
        help="Shape the synthetic values to the place: storeys from the tagged "
        "buildings' mix, m² per dwelling so each block's dwellings equal the census",
    )
    args = parser.parse_args(argv)

    result = seed(
        args.dsn,
        aoi_id=args.aoi_id,
        dry_run=args.dry_run,
        fit_to_census=args.fit_to_census,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
