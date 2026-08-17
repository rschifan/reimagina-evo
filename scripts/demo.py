#!/usr/bin/env python
"""The orchestration demo, on real Tlalpan data.

Answers the four questions the Reimagina collaborators asked, by running them
rather than describing them:

  mapping        which attribute edits trigger which runs
  one-to-many    one dispatched unit producing several indicators
  status         per-indicator state, observable while the run is in flight
  allocation     the cheap and expensive families, and why they differ

Two commands, because the interesting half should be re-runnable without
repeating the slow half:

    python scripts/demo.py setup   # one-time: ingest, AOI, amenities, network
    python scripts/demo.py run     # the demo itself, seconds

`setup` is not idempotent in the cheap sense — it re-ingests. Point it at a
scratch database, not one you care about.

⚠ Requires DATABASE_URL_DIRECT and REIMAGINA_DBOS_EXECUTION_MODE=dev, since the
demo submits and executes in one process. `run` sets them itself if unset.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from uuid import uuid4

import psycopg2

DEFAULT_DSN = "postgresql://localhost/reimagina_demo"
#: Where the real INEGI / SIGCDMX extracts live. Not in this worktree.
DEFAULT_SOURCE_ROOT = "/Users/rschifan/Desktop/projects/reimagina/data/sources/azure"
AOI_NAME = "Tlalpan"
CVE_ENT, CVE_MUN = "09", "012"


# ── presentation ────────────────────────────────────────────────────────────


def head(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "─" * len(text))


def step(text: str) -> None:
    print(f"  · {text}")


# ── setup ───────────────────────────────────────────────────────────────────


def _cli(*args: str, dsn: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "apps.cli.app", *args, "--dsn", dsn],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def setup(dsn: str, source_root: str) -> None:
    head("Setup — real data into a fresh database")

    step("INEGI marco geoestadístico: CDMX municipalities + urban blocks")
    _cli(
        "inegi-marco",
        "bootstrap",
        "--layers",
        "areas_municipales,manzanas_urbanas",
        "--cve-ent",
        CVE_ENT,
        "--source-root",
        f"{source_root}/inegi/marco_geostadistico/2024",
        dsn=dsn,
    )

    step(f"AOI from alcaldía {CVE_ENT}/{CVE_MUN} → {AOI_NAME}")
    _cli(
        "cdmx",
        "aoi-from-alcaldia",
        "--cve-ent",
        CVE_ENT,
        "--cve-mun",
        CVE_MUN,
        "--name",
        AOI_NAME,
        dsn=dsn,
    )

    step("SIGCDMX amenities for Tlalpan")
    _cli(
        "points",
        "bootstrap",
        "--layers",
        "tlalpan_amenities",
        "--source-root",
        source_root,
        dsn=dsn,
    )

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    aoi_id = _aoi_id(conn)

    step("fixture attributes (num_levels, unit_size, people_per_household, …)")
    subprocess.run(
        [
            sys.executable,
            "scripts/seed_demo_attributes.py",
            "--dsn",
            dsn,
            "--aoi-id",
            aoi_id,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    step("indicator catalog")
    from reimagina.indicators.catalog import seed_catalog

    seed_conn = psycopg2.connect(dsn)
    seed_catalog(seed_conn)
    seed_conn.commit()
    seed_conn.close()

    step("OSM walking network via Overpass — minutes, not seconds")
    from reimagina.pipeline.osm_network import bootstrap_network

    started = time.time()
    result = bootstrap_network(conn, aoi_id=aoi_id)
    step(
        f"  {result['nodes']:,} nodes, {result['edges']:,} edges, "
        f"{result['total_length_km']:,} km in {time.time() - started:.0f}s"
    )
    conn.close()

    print("\nSetup complete. Now: python scripts/demo.py run")


# ── the demo ────────────────────────────────────────────────────────────────


def _aoi_id(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM curated_geo.area_of_interest WHERE name = %s",
            (AOI_NAME,),
        )
        row = cur.fetchone()
    if row is None:
        sys.exit(f"no AOI named {AOI_NAME!r} — run `demo.py setup` first")
    return row[0]


def _inventory(conn, aoi_id: str) -> None:
    head("What is in the database")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM curated_geo.feature f
            JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
            JOIN curated_geo.area_of_interest a ON a.id = %s::uuid
                 AND ST_Intersects(f.geometry, a.geometry)
            WHERE t.type_slug = 'block'
            """,
            (aoi_id,),
        )
        blocks = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM curated_geo.feature WHERE type_slug = 'establishment'"
        )
        amenities = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM curated_geo.road_node")
        nodes = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*), round(sum(cost)::numeric/1000) FROM curated_geo.road_edge"
        )
        edges, km = cur.fetchone()
    print(f"  blocks (Tlalpan)   {blocks:>8,}   real INEGI manzanas urbanas")
    print(f"  amenities          {amenities:>8,}   real SIGCDMX points")
    print(f"  network nodes      {nodes:>8,}   real OSM pedestrian graph")
    print(f"  network edges      {edges:>8,}   {km:,} km")


def _catalog(conn) -> None:
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    head("The catalog — 13 indicators in 4 dispatch groups")
    groups: dict[tuple[str, str], list[str]] = {}
    for spec in SHIPPED_INDICATORS:
        key = (spec.run_group or spec.slug, spec.resource_class)
        groups.setdefault(key, []).append(spec.slug)
    for (group, cls), slugs in sorted(groups.items()):
        marker = "HEAVY" if cls == "heavy" else "     "
        print(f"  {marker}  {group:<28} {len(slugs)} indicator(s)")
        for slug in slugs:
            print(f"         └─ {slug}")


def _mapping(conn) -> None:
    """Ask 3: which attribute edits trigger which runs — from the declarations."""
    from reimagina.indicators.catalog import SHIPPED_INDICATORS
    from reimagina.orchestration.impact import compute_dirty_set

    head("Ask 3 · mapping — attribute edited → indicators invalidated")
    by_slug = {s.slug: s for s in SHIPPED_INDICATORS}
    for attribute in ("num_levels", "poi_version", "network_version", "geometry"):
        units = compute_dirty_set([attribute], [], SHIPPED_INDICATORS)
        groups: dict[str, list[str]] = {}
        for unit in units:
            spec = by_slug[unit.indicator]
            groups.setdefault(spec.run_group or unit.indicator, []).append(
                unit.indicator
            )
        classes = {by_slug[u.indicator].resource_class for u in units}
        cls = "heavy" if "heavy" in classes else "default"
        print(
            f"  {attribute:<17} → {len(units)} indicators in "
            f"{len(groups)} dispatched unit(s)   [{cls}]"
        )


def _run_and_watch(conn, orchestrator, *, attribute: str, aoi_id: str) -> str:
    """Submit one edit and print each indicator as it settles."""
    from reimagina.orchestration.port import RUN_TERMINAL_STATES

    handle = orchestrator.submit_run(
        conn,
        changed_attributes=[attribute],
        edited_features=[],
        aoi_id=aoi_id,
        code_version="demo",
    )
    started = time.time()
    seen: dict[str, str] = {}
    while True:
        status = orchestrator.get_run(conn, handle.run_id)
        if status is not None:
            for unit in status.units:
                if seen.get(unit.indicator) != unit.status:
                    seen[unit.indicator] = unit.status
                    if unit.status in {"succeeded", "failed", "skipped"}:
                        print(
                            f"      {time.time() - started:6.1f}s  "
                            f"{unit.status:<10} {unit.indicator}"
                        )
            if status.status in RUN_TERMINAL_STATES:
                print(
                    f"      {time.time() - started:6.1f}s  run {status.status} "
                    f"({len(status.units)} indicators)"
                )
                return handle.run_id
        if time.time() - started > 900:
            sys.exit(f"run {handle.run_id} did not finish in 15 minutes")
        time.sleep(0.05)


def _children(conn, run_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM dbos.workflow_status "
            "WHERE workflow_uuid LIKE %s AND name LIKE %s",
            (f"{run_id}-%", "%unit_workflow%"),
        )
        return cur.fetchone()[0]


def _values(conn, aoi_id: str) -> None:
    head("The results — real values on real blocks")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.indicator_slug,
                   count(*),
                   round(min(r.value_numeric)::numeric, 1),
                   round(avg(r.value_numeric)::numeric, 1),
                   round(max(r.value_numeric)::numeric, 1)
            FROM curated_geo.indicator_result r
            WHERE r.status = 'current' AND r.scenario_id IS NULL
            GROUP BY 1 ORDER BY 1
            """
        )
        rows = cur.fetchall()
    print(f"  {'indicator':<46}{'blocks':>8}{'min':>9}{'mean':>9}{'max':>9}")
    for slug, n, lo, avg, hi in rows:
        print(f"  {slug:<46}{n:>8,}{lo:>9}{avg:>9}{hi:>9}")

    # The free correctness check: routing must never beat a straight line.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE n.value_numeric < e.value_numeric - 0.5)
            FROM curated_geo.indicator_result e
            JOIN curated_geo.indicator_result n
              ON n.feature_id = e.feature_id AND n.status = 'current'
             AND n.indicator_slug = replace(e.indicator_slug, '_euclid_m', '_network_m')
            WHERE e.status = 'current' AND e.indicator_slug LIKE '%_euclid_m'
            """
        )
        violations = cur.fetchone()[0]
    verdict = "OK" if violations == 0 else f"{violations} VIOLATIONS"
    print(f"\n  network distance >= straight-line distance everywhere: {verdict}")


def run(dsn: str) -> None:
    os.environ.setdefault("DATABASE_URL_DIRECT", dsn)
    os.environ.setdefault("REIMAGINA_DBOS_EXECUTION_MODE", "dev")
    os.environ.setdefault("REIMAGINA_EXECUTOR_ID", f"demo-{uuid4().hex[:8]}")

    from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    aoi_id = _aoi_id(conn)

    _inventory(conn, aoi_id)
    _catalog(conn)
    _mapping(conn)

    orchestrator = DBOSOrchestrator()

    head("Ask 2+4 · dispatch and status — one edit at a time")
    for attribute in ("num_levels", "poi_version", "network_version"):
        print(f"\n  edit `{attribute}` across the AOI:")
        run_id = _run_and_watch(conn, orchestrator, attribute=attribute, aoi_id=aoi_id)
        children = _children(conn, run_id)
        indicators = len(orchestrator.get_run(conn, run_id).units)
        print(
            f"      → {indicators} indicators from {children} dispatched "
            f"unit(s)  ({indicators // max(children, 1)}x fan-out)"
        )

    _values(conn, aoi_id)
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("setup", "run"))
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)
    args = parser.parse_args()

    if args.command == "setup":
        setup(args.dsn, args.source_root)
    else:
        run(args.dsn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
