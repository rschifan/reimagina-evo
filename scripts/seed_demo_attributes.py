#!/usr/bin/env python
"""Seed the fixture attributes the edificability and accessibility demos read.

# ponytail: fixture data. Delete this script when a real cadastre and a real
# zoning table land — everything here exists because nothing in the shipped
# pipeline populates a scenario-editable attribute today.

That gap is worth stating plainly, because it is not obvious: INEGI's marco
geoestadístico carries only identifiers (`CVE_ENT`, `CVE_MUN`, `CVE_AGEB`, …),
Overture's `num_floors` lands on `global:building` rather than on blocks, and
`infra/db/seeds/` is empty. `overture block-storeys` writes a block's storeys
as a `floors` observation (83 Mexicali blocks are tagged), and nothing writes
`num_levels`, `unit_size` or `lot_coverage`, so the edificability family
returns zero rows against a freshly bootstrapped database without this script.

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

**Fitted to real buildings and the census (``--fit-to-census``).** The flat
values above are generic, and on a real city they are absurd: over Mexicali
they gave 13.8 million potential dwellings against the census's 340,169.
Fitted mode derives every input from what the install loaded, so each term of

    housing_units = block_area × lot_coverage × num_levels ÷ unit_size
    population    = housing_units × people_per_household

means what the scenario form says it means, and the formula gives each block's
census dwellings and population back exactly:

    lot_coverage          the union of the Overture footprints clipped to
                          the block (an overlap counts once) ÷ block area
                          (geodesic), capped at 1
    num_levels / floors   the block's ``floors`` observation — the
                          footprint-weighted mean storeys, untagged buildings
                          counting one (``reimagina.pipeline.block_storeys``)
                          — else 1
    unit_size             built floor area (footprint × storeys) ÷ census
                          dwellings (``censo_2020_vivtot``): m² per dwelling
    people_per_household  census population ÷ census dwellings
                          (``censo_2020_pobtot`` ÷ ``censo_2020_vivtot``)

So a planner's "80 m² per dwelling, coverage 0.6, 4 storeys" is compared with
the block's own m² per dwelling, coverage and storeys, not with a gross land
share per dwelling that had coverage, yards and shops folded into it.

⚠ **People per household is per dwelling, occupied or not.** The census
dwelling count includes empty ones, and INEGI's ``prom_ocup`` divides by
occupied ones only; multiplying the two overstated Mexicali's population by
13.8%. Folding the occupancy into this one ratio keeps the formula at two
terms and the baseline at the census population. A rule's value is read the
same way, so a planner typing household size gets a fully occupied block.

⚠ **A block without census dwellings gets no ``unit_size``**, so the formula
skips it, as it skips any block missing an input, rather than inventing homes
on a park or a factory. Its people per household is the city's ratio, so a
rule that does set a dwelling size there has a population to compute.

⚠ **A block with dwellings but no mapped footprint** (coverage under 0.0001:
Overture misses some buildings) takes the median coverage of the blocks that
have both, so its dwellings still come out at the census count. So does a
block whose own footprint leaves under ``MIN_UNIT_SIZE_M2`` (20 m²) per
dwelling: Overture mapped only part of its buildings (Mexicali: 103 blocks,
e.g. coverage 0.047 for 23 dwellings), and 5 m² per dwelling is a mapping gap,
not a block of cupboards. The median is taken over the blocks that pass the
floor. Both kinds are marked ``estimated`` in the attributes, with the reason
(``no_footprint`` / ``thin_footprint``), and the overlay tile carries it so the
map says which blocks' inputs are guesses.

⚠ **Rounded where a sum of floats feeds it.** Coverage is rounded to 0.0001
and storeys to 0.01 (in ``block_storeys``), because the last bits of a
geometric sum can follow the query plan or the input order; ``unit_size`` is then computed from the rounded values,
so it still reproduces the census and two installs store the same numbers.

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
from typing import NamedTuple

import psycopg2
import psycopg2.extras  # execute_batch lives on the extras module, not the root

from reimagina.pipeline.block_storeys import CLIPPED_FOOTPRINT

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost/reimagina")

#: Fixed for every block — only num_levels varies.
CONSTANT_ATTRIBUTES = {
    "unit_size": 60,
    "people_per_household": 3.6,
    "poi_version": 1,
    "network_version": 1,
}

#: Fewer m² per dwelling than this, from a block's own footprint, means
#: Overture mapped only part of its buildings: the block is fitted like an
#: unmapped one. 20 m² is the owner's plausibility floor: smaller than a
#: studio flat, and on Mexicali it catches 103 partly mapped blocks (one has
#: 4.6 m² per dwelling: coverage 0.047 for 23 dwellings).
MIN_UNIT_SIZE_M2 = 20.0

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


def _fitted_payloads(cur, feature_ids: list[str]) -> list[dict[str, object]]:
    """Per-block values fitted to buildings and the census; see the docstring."""

    def current(key: str) -> str:
        return (
            "(SELECT o.value_numeric FROM curated_geo.observation o "
            f"WHERE o.feature_id = blk.id AND o.attribute_key = '{key}' AND o.is_current)"
        )

    # Area as the indicator measures it (geodesic), or the fit is off by the
    # projection's scale and the census count is not reproduced. Footprint is
    # the area of the *union* of the clipped buildings: Overture carries
    # overlapping and duplicated footprints, and a sum counted them twice
    # (coverage is then capped at 1 by `_coverage`). Ordered so the union, and
    # its area to the last bit, is the same on every install.
    cur.execute(
        f"""
        SELECT blk.id::text, ST_Area(blk.geometry::geography),
               COALESCE((
                   SELECT ST_Area(ST_Union({CLIPPED_FOOTPRINT}
                                           ORDER BY b.source_record_ref, b.id)
                                  ::geography)
                   FROM curated_geo.feature b
                   WHERE b.type_slug = 'building'
                     AND ST_Intersects(blk.geometry, b.geometry)
               ), 0),
               {current("censo_2020_vivtot")}, {current("censo_2020_pobtot")},
               {current("floors")}
        FROM curated_geo.feature blk
        WHERE blk.id = ANY(%s::uuid[])
        """,
        (feature_ids,),
    )
    return fit_payloads(cur.fetchall())


class _Block(NamedTuple):
    """One block's inputs to the fit, each derived once."""

    fid: str
    area: float
    dwellings: float | None
    population: float | None
    levels: float
    coverage: float
    #: Has dwellings and an area: the fit sets its unit size.
    housed: bool
    #: Why a housed block's coverage must be estimated, or None.
    gap: str | None


def fit_payloads(
    blocks: list[tuple[str, float, float, float | None, float | None, float | None]],
) -> list[dict[str, object]]:
    """Fitted values for ``(id, area, footprint, dwellings, population, storeys)``."""
    fits = []
    for fid, area, footprint, dwellings, population, storeys in blocks:
        levels = float(storeys) if storeys else 1.0
        coverage = _coverage(area, footprint) if area > 0 else 0.0
        housed = bool(dwellings and dwellings > 0 and area > 0)
        gap = _gap(area, coverage, levels, dwellings) if housed and dwellings else None
        fits.append(
            _Block(fid, area, dwellings, population, levels, coverage, housed, gap)
        )
    covered = sorted(b.coverage for b in fits if b.housed and b.gap is None)
    fallback_coverage = covered[len(covered) // 2] if covered else 1.0
    counted = [
        (b.dwellings or 0.0, b.population)
        for b in fits
        if b.housed and b.population is not None
    ]
    city_dwellings = sum(d for d, _ in counted)
    city_ratio = (
        round(sum(p for _, p in counted if p is not None) / city_dwellings, 4)
        if city_dwellings
        else CONSTANT_ATTRIBUTES["people_per_household"]
    )

    payloads = []
    for b in fits:
        coverage = b.coverage
        payload: dict[str, object] = {
            "feature_id": b.fid,
            "poi_version": CONSTANT_ATTRIBUTES["poi_version"],
            "network_version": CONSTANT_ATTRIBUTES["network_version"],
            # Same value, two keys -- see the module docstring.
            "floors": b.levels,
            "num_levels": b.levels,
            "people_per_household": city_ratio,
        }
        if b.housed and b.dwellings:
            if b.gap is not None:
                coverage = fallback_coverage
                payload["estimated"] = b.gap
            payload["unit_size"] = b.area * coverage * b.levels / b.dwellings
            if b.population is not None:
                payload["people_per_household"] = b.population / b.dwellings
        payload["lot_coverage"] = coverage
        payloads.append(payload)
    return payloads


def _coverage(area: float, footprint: float) -> float:
    return round(min(1.0, float(footprint) / float(area)), 4)


def _gap(area: float, coverage: float, levels: float, dwellings: float) -> str | None:
    """Why a housed block's coverage must be estimated, or None if it need not."""
    if coverage == 0:
        return "no_footprint"
    if area * coverage * levels / dwellings < MIN_UNIT_SIZE_M2:
        return "thin_footprint"
    return None


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
                # Minus unit_size and estimated first: a block that has none
                # in this fit must not keep one from an earlier seed.
                "UPDATE curated_geo.feature "
                "SET attributes = (attributes - 'unit_size' - 'estimated') "
                "|| %s::jsonb, "
                "updated_at = now() "
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
        help="Fit the inputs to the place: coverage and storeys from the "
        "buildings, m² per dwelling and people per household from the census",
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
