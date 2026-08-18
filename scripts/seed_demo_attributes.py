#!/usr/bin/env python
"""Seed the fixture attributes the edificability and accessibility demos read.

# ponytail: fixture data. Delete this script when a real cadastre and a real
# zoning table land — everything here exists because nothing in the shipped
# pipeline populates a scenario-editable attribute today.

That gap is worth stating plainly, because it is not obvious: INEGI's marco
geoestadístico carries only identifiers (`CVE_ENT`, `CVE_MUN`, `CVE_AGEB`, …),
Overture's `num_floors` lands on `global:building` rather than on blocks, and
`infra/db/seeds/` is empty. The one attribute any shipped indicator reads —
`floors`, for `urban_form_block_planned_floors` — is written by *tests* and
nothing else, so that indicator returns zero rows against a freshly
bootstrapped database. These indicators would inherit the same emptiness.

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
the feature id, so re-running produces identical values and two developers see
the same map. An unseeded RNG here would make every run a different dataset and
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

Usage:

    python scripts/seed_demo_attributes.py --aoi-id <uuid>
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


def levels_for(feature_id: str) -> int:
    """A stable 1–6 storey count for a feature id.

    blake2b rather than `hash()`: the builtin is salted per process by
    PYTHONHASHSEED, so it would hand out different values on every run — which
    is precisely the non-determinism this script exists to avoid.
    """
    digest = hashlib.blake2b(feature_id.encode("utf-8"), digest_size=8).digest()
    return MIN_LEVELS + int.from_bytes(digest, "big") % (MAX_LEVELS - MIN_LEVELS + 1)


def _block_ids(cur, aoi_id: str | None) -> list[str]:
    """Every MEX:block feature, optionally restricted to one AOI.

    The AOI join mirrors `FeatureRepository.geodesic_metrics_by_type`, so the
    seeded set and the set the indicators later compute over are the same.
    """
    if aoi_id is None:
        cur.execute("""
            SELECT f.id::text
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
            WHERE t.context = 'MEX' AND t.type_slug = 'block'
        """)
    else:
        cur.execute(
            """
            SELECT f.id::text
            FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
            JOIN curated_geo.area_of_interest aoi
              ON aoi.id = %s::uuid AND ST_Intersects(f.geometry, aoi.geometry)
            WHERE t.context = 'MEX' AND t.type_slug = 'block'
            """,
            (aoi_id,),
        )
    return [row[0] for row in cur.fetchall()]


def seed(dsn: str, *, aoi_id: str | None, dry_run: bool) -> dict[str, object]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            feature_ids = _block_ids(cur, aoi_id)
            if not feature_ids:
                return {
                    "blocks": 0,
                    "written": 0,
                    "note": (
                        "no MEX:block features found — ingest the INEGI marco "
                        "geoestadístico first, and check the --aoi-id"
                    ),
                }

            payloads = [
                (
                    json.dumps(
                        {
                            **CONSTANT_ATTRIBUTES,
                            # Same value, two keys -- see the module docstring.
                            "num_levels": levels_for(fid),
                            "floors": levels_for(fid),
                        }
                    ),
                    fid,
                )
                for fid in feature_ids
            ]
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
    args = parser.parse_args(argv)

    result = seed(args.dsn, aoi_id=args.aoi_id, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
