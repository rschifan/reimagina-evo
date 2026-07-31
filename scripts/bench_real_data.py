"""Run the shipped indicator DAG over real city data, on each ops-plane.

The workload suite measures how an engine *schedules*, using synthetic units
whose cost is a sleep or a spin. That is deliberate — it makes DAG shapes the
four-indicator catalog cannot produce, and it is reproducible on any machine.
What it cannot show is the thing a real deployment actually hits:

    every unit contends for the same Postgres.

A synthetic unit that sleeps scales perfectly across slots. Three real PostGIS
statements against one server do not, and how far short they fall is the number
that decides whether more worker slots buy anything. This script measures that,
by running the **real** ``SHIPPED_INDICATORS`` runners over **real** features.

⚠ It writes ``indicator_result`` rows under a fresh ``scenario_id`` per
repetition, never as baseline. The partial unique indexes keep base and scenario
rows apart (``ir_one_current_base`` / ``ir_one_current_scenario``), so the
city's committed baseline is untouched by anything here.

Usage::

    DATABASE_URL_DIRECT=postgresql://localhost/reimagina_real_b \\
      python scripts/bench_real_data.py --reps 3

`hatchet` mode additionally needs `HATCHET_CLIENT_TOKEN` and a worker already
registered against the same database — this branch has no in-process mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "py"))

from reimagina.indicators.catalog import SHIPPED_INDICATORS  # noqa: E402
from reimagina.orchestration.factory import get_orchestrator  # noqa: E402
from reimagina.orchestration.port import RUN_TERMINAL_STATES  # noqa: E402
from reimagina.perf.workload_report import (  # noqa: E402
    aggregate,
    db_now,
    duplicate_executions,
    measure,
    ordering_violations,
    unit_records,
)

# A geometry edit dirties compactness + building_density (and the composite that
# depends on them); a floors edit dirties planned_floors. Both together are what
# makes the run four units wide instead of three.
CHANGED_ATTRIBUTES = frozenset({"geometry", "floors"})

EDGES = {
    spec.slug: tuple(dep for dep, _weight in spec.depends_on)
    for spec in SHIPPED_INDICATORS
}

SETTLE_TIMEOUT_S = 900.0
POLL_S = 0.05


def block_ids(conn, limit: int | None) -> list[str]:
    """Real block features, in a stable order so two runs edit the same set."""
    sql = (
        "SELECT f.id::text FROM curated_geo.feature f "
        "JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id "
        "WHERE t.type_slug = 'block' ORDER BY f.id"
    )
    with conn.cursor() as cur:
        cur.execute(sql + (" LIMIT %s" if limit else ""), (limit,) if limit else ())
        return [row[0] for row in cur.fetchall()]


def await_terminal(orchestrator, conn, run_id: str):
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        # Roll back between polls or this connection holds a snapshot from
        # before the units committed, and the job_run read below sees nothing.
        conn.rollback()
        last = orchestrator.get_run(run_id)
        if last is not None and last.status in RUN_TERMINAL_STATES:
            return last, db_now(conn)
        time.sleep(POLL_S)
    raise SystemExit(
        f"run {run_id} did not settle in {SETTLE_TIMEOUT_S:.0f}s "
        f"(last status: {last.status if last else None})"
    )


def run_once(mode: str, conn, features: list[str], slots: int) -> dict:
    orchestrator = get_orchestrator(mode)
    # Fresh per repetition: `code_version` keeps the idempotency window from
    # folding repetitions onto one run, and `scenario_id` keeps them out of a
    # single `max_runs=1` concurrency bucket. Both were learned the hard way in
    # the synthetic harness; neither is optional here.
    code_version = f"real-{uuid.uuid4().hex[:8]}"
    scenario_id = str(uuid.uuid4())

    t_submit = db_now(conn)
    handle = orchestrator.submit_run(
        conn,
        changed_attributes=set(CHANGED_ATTRIBUTES),
        edited_features=set(features),
        scenario_id=scenario_id,
        code_version=code_version,
    )
    if handle.status is not None:  # a blocking implementation, e.g. `sync`
        status, t_terminal = handle.status, db_now(conn)
    else:
        status, t_terminal = await_terminal(orchestrator, conn, handle.run_id)

    records = unit_records(conn, handle.run_id)
    return {
        "run_id": handle.run_id,
        "status": status.status if status else None,
        "unit_statuses": {
            u.indicator: u.status for u in (status.units if status else ())
        },
        "records": records,
        "measured": measure(
            records=records,
            edges=EDGES,
            t_submit=t_submit,
            t_terminal=t_terminal,
            slots=slots,
        ),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=int, default=0, help="blocks; 0 = every block")
    parser.add_argument("--reps", type=int, default=3, help="first is warm-up")
    parser.add_argument("--modes", default="sync,hatchet")
    parser.add_argument(
        "--slots",
        type=int,
        default=int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "4")),
    )
    parser.add_argument("--out", default="artifacts/workloads-real-hatchet.json")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        parser.error("DATABASE_URL_DIRECT is required — it is the database measured")

    from reimagina.db import connect

    conn = connect(dsn)
    features = block_ids(conn, args.scope or None)
    if not features:
        parser.error("no block features found — is this a populated database?")
    print(f"scope: {len(features)} blocks, {len(EDGES)} indicators, slots={args.slots}")

    doc: dict = {
        "scope_features": len(features),
        "slots": args.slots,
        "reps": args.reps,
        "warm": args.reps > 1,
        "changed_attributes": sorted(CHANGED_ATTRIBUTES),
        "modes": {},
    }

    for mode in args.modes.split(","):
        mode = mode.strip()
        reps = []
        for rep in range(args.reps):
            result = run_once(mode, conn, features, args.slots)
            violations = ordering_violations(result["records"], EDGES)
            duplicates = duplicate_executions(result["records"])
            if violations:
                print(f"  ⚠ ordering violation: {violations}")
            if duplicates:
                print(f"  ⚠ unit body ran twice: {duplicates}")
            m = result["measured"]
            tag = "warm-up" if rep == 0 and args.reps > 1 else f"rep {rep}"
            print(
                f"  {mode:8} {tag:8} {result['status']:9} "
                f"makespan={m['makespan_ms']:8.0f}ms  "
                f"busy={m['busy_ms']:8.0f}ms  "
                f"parallelism={m['observed_parallelism']:5.2f}  "
                f"units={m['units_timed']}"
            )
            reps.append(m)
        # The first repetition pays one-time costs — a cold worker, a freshly
        # declared workflow, a cold page cache — so it is recorded and excluded.
        doc["modes"][mode] = aggregate(reps[1:] if len(reps) > 1 else reps)
        doc["modes"][mode]["warmup"] = reps[0] if len(reps) > 1 else None

    baseline = doc["modes"].get("sync", {}).get("makespan_ms")
    for mode, metrics in doc["modes"].items():
        if mode != "sync" and baseline and metrics.get("makespan_ms"):
            metrics["speedup"] = round(baseline / metrics["makespan_ms"], 3)
            metrics["efficiency"] = round(metrics["speedup"] / args.slots, 3)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str))
    print(f"\nreal-data report -> {out}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
