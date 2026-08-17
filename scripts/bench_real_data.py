"""Run the shipped indicator DAG over real city data, on each ops-plane.

The workload suite measures how an engine *schedules*, using synthetic units
whose cost is a sleep or a spin. That is deliberate — it is reproducible on any
machine. What it cannot show is what a real deployment actually hits, and the
list has grown since it was written:

    every unit contends for the same Postgres
    units cost wildly different amounts
    several indicators share one dispatched unit

A synthetic unit that sleeps scales perfectly across slots. Real PostGIS
statements against one server do not, and how far short they fall is the number
that decides whether more worker slots buy anything. This script measures that,
by running the **real** ``SHIPPED_INDICATORS`` runners over **real** features.

The other two are newer, and are why this script is now the more interesting of
the pair. The synthetic workloads declare no ``run_group`` and no
``resource_class``, so grouping and allocation are invisible there *by
construction* — every synthetic node is one unit costing its declared sleep. The
real catalog is 13 indicators in 7 dispatched units, three of them heavy, with a
straight-line KNN and a multi-source Dijkstra differing by more than a factor of
two. That spread is the whole argument for scheduling them apart, and only this
script can see it — it is also what corrected the ``resource_class`` labels,
which were assigned the wrong way round from intuition about what each
computation sounds like.

The suite's own note that the shipped catalog "cannot expose where they differ"
was written when it was four indicators: three independent roots and one sink,
where every strategy plans identically. That is no longer true.

⚠ It writes ``indicator_result`` rows under a fresh ``scenario_id`` per
repetition, never as baseline. The partial unique indexes keep base and scenario
rows apart (``ir_one_current_base`` / ``ir_one_current_scenario``), so the
city's committed baseline is untouched by anything here.

Usage::

    DATABASE_URL_DIRECT=postgresql://localhost/reimagina_real_b \\
      python scripts/bench_real_data.py --reps 3

The engine mode is taken from ``factory.DEFAULT_MODE``, so this script is
byte-identical on both worktrees. The engine mode needs whatever that engine
needs — a registered worker against the same database, and for Hatchet a
``HATCHET_CLIENT_TOKEN``; neither branch has an in-process durable mode.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "py"))

from reimagina.indicators.catalog import SHIPPED_INDICATORS  # noqa: E402
from reimagina.orchestration.factory import (  # noqa: E402
    DEFAULT_MODE,
    get_orchestrator,
)
from reimagina.orchestration.port import RUN_TERMINAL_STATES  # noqa: E402
from reimagina.perf.workload_report import (  # noqa: E402
    aggregate,
    db_now,
    duplicate_executions,
    measure,
    ordering_violations,
    unit_records,
)

# Every attribute the shipped catalog reads, so one submit dirties the WHOLE
# catalog. Derived rather than listed: a new indicator declaring a new `reads`
# key joins the benchmark automatically, which is the only way this set stays
# honest.
#
# ⚠ This was `{"geometry", "floors"}` — correct when the catalog was four
# indicators, silently wrong afterwards. Against today's 13 it dirties 4
# indicators in 4 units with nothing heavy, skipping all nine edificability and
# accessibility indicators, all four run groups, and both resource classes. The
# benchmark kept reporting, so nothing looked broken.
CHANGED_ATTRIBUTES = frozenset(key for spec in SHIPPED_INDICATORS for key in spec.reads)

EDGES = {
    spec.slug: tuple(dep for dep, _weight in spec.depends_on)
    for spec in SHIPPED_INDICATORS
}

#: slug -> the unit it is dispatched in, and how expensive that unit is meant
#: to be. Both are what the synthetic suite cannot model: its workloads declare
#: no `run_group` and no `resource_class`, so grouping and allocation are
#: invisible there by construction.
GROUP_OF = {spec.slug: (spec.run_group or spec.slug) for spec in SHIPPED_INDICATORS}
CLASS_OF = {spec.slug: spec.resource_class for spec in SHIPPED_INDICATORS}

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


def _get_run(orchestrator, conn, run_id: str):
    """Read a run, on either track's `get_run`.

    ⚠ The two tracks' ports have **forked on this method**, which is why this
    shim exists rather than a direct call:

        Track A   get_run(conn, run_id)   — matches `main`
        Track B   get_run(run_id)         — its own port.py drops `conn`

    Track B has no run tables, so its `conn` was genuinely unused; it removed
    the parameter from its port rather than keeping it ignored. That is a
    defensible local decision and it breaks every piece of code meant to run
    against both, this benchmark included.

    Bound by signature, once, rather than by catching TypeError per poll: a
    `TypeError` raised *inside* a correct call would otherwise be swallowed and
    retried as if it were an arity mismatch.
    """
    if "conn" in inspect.signature(orchestrator.get_run).parameters:
        return orchestrator.get_run(conn, run_id)
    return orchestrator.get_run(run_id)


def await_terminal(orchestrator, conn, run_id: str):
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        # Roll back between polls or this connection holds a snapshot from
        # before the units committed, and the job_run read below sees nothing.
        conn.rollback()
        last = _get_run(orchestrator, conn, run_id)
        if last is not None and last.status in RUN_TERMINAL_STATES:
            return last, db_now(conn)
        time.sleep(POLL_S)
    raise SystemExit(
        f"run {run_id} did not settle in {SETTLE_TIMEOUT_S:.0f}s "
        f"(last status: {last.status if last else None})"
    )


def per_indicator(records) -> dict[str, dict]:
    """Wall-clock per indicator, plus the unit and class it was dispatched in.

    The headline makespan hides which indicator actually costs the run, and on
    this catalog that is the whole question — the spread across indicators is
    what `resource_class` exists to schedule around. A synthetic workload whose
    every unit sleeps for its declared duration cannot show it, which is how the
    labels came to be assigned backwards in the first place.
    """
    out: dict[str, dict] = {}
    for record in records:
        if record.started_at is None or record.completed_at is None:
            continue
        out[record.slug] = {
            "ms": round(
                (record.completed_at - record.started_at).total_seconds() * 1000, 1
            ),
            "group": GROUP_OF.get(record.slug, record.slug),
            "class": CLASS_OF.get(record.slug, "default"),
        }
    return out


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
    # ⚠ Commit, and it is not bookkeeping. `sync` runs every unit inside *this*
    # transaction and never commits on its own, so without this the whole run —
    # results and `job_run` rows alike — is discarded when the connection
    # closes. Two things were wrong as a result: nothing persisted for anyone to
    # check afterwards, and the comparison was not like-for-like, because the
    # engine-backed modes each paid a real commit per unit on their own
    # connections while `sync` paid none.
    conn.commit()
    return {
        "run_id": handle.run_id,
        "status": status.status if status else None,
        "unit_statuses": {
            u.indicator: u.status for u in (status.units if status else ())
        },
        "records": records,
        "per_indicator": per_indicator(records),
        "measured": measure(
            records=records,
            edges=EDGES,
            t_submit=t_submit,
            t_terminal=t_terminal,
            slots=slots,
        ),
    }


# ⚠ Every repetition writes one full scope of `indicator_result` — 258,675 rows
# per indicator on CDMX — under a fresh `scenario_id`, and nothing ever removed
# them. The table is what every unit then queries, so each run made the next one
# slower and the effect compounded silently across months.
#
# Measured when this was found: `reimagina_real_b` had 34 accumulated scenarios
# and a 3,048 MB `indicator_result`; `reimagina_real_a` had 10 and 1,256 MB. The
# same `sync` control read 64.8s on one database and 27.1s on the other — a 2.4x
# spread with identical code over identical features. Every cross-track number
# taken before this swept was comparing benchmark history, not engines.
#
# ⚠ `scenario_id IS NOT NULL` is load-bearing, not defensive. The baseline rows
# carry a `real-<track>-<hex>` model_version too — `real-dbos-b4ac6de8`,
# `real-hatchet-3caddac4` — so matching on the prefix alone deletes the 258,675
# committed baseline rows that every correctness check compares against, and
# they cost a full recompute to rebuild. The scenario predicate is what makes
# the prefix safe.
BENCH_ROW_PREDICATE = """
    scenario_id IS NOT NULL
    AND (model_version LIKE 'real-%%' OR model_version LIKE 'verify-%%')
"""


def sweep_bench_rows(dsn: str) -> int:
    """Remove this benchmark's own leftovers, then hand the planner the truth.

    On the way **in**, matching `tests/workloads/conftest.py`: a killed run
    leaves rows behind and a teardown hook never gets to argue about it.

    ⚠ `VACUUM` is not optional here and it cannot run inside a transaction. A
    bare DELETE leaves dead tuples, so the table keeps its physical size and the
    next run reads exactly the pages this was meant to remove — the sweep would
    look like it worked and change nothing. ANALYZE then matters for its own
    reason: the row estimate drives the join plan, and this suite has already
    lost 18 minutes once to a planner sizing a query for a table a fraction of
    its real size.
    """
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM curated_geo.indicator_result WHERE {BENCH_ROW_PREDICATE}"
            )
            removed = cur.rowcount
            cur.execute("VACUUM ANALYZE curated_geo.indicator_result")
        return removed
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=int, default=0, help="blocks; 0 = every block")
    parser.add_argument("--reps", type=int, default=3, help="first is warm-up")
    # ⚠ Derived from the factory, never hardcoded. `DEFAULT_MODE` is the one
    # place each branch names its engine, so this file stays byte-identical on
    # both worktrees — the same property `perf/workloads.py` depends on, and for
    # the same reason: a benchmark that differs between the tracks it compares
    # is measuring itself.
    parser.add_argument("--modes", default=f"sync,{DEFAULT_MODE}")
    parser.add_argument(
        "--slots",
        type=int,
        default=int(os.environ.get("REIMAGINA_UNIT_CONCURRENCY", "4")),
    )
    parser.add_argument(
        "--out", default=f"artifacts/workloads-real-{DEFAULT_MODE}.json"
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        parser.error("DATABASE_URL_DIRECT is required — it is the database measured")

    # ⚠ `--slots` has to *set* the slot count, not just report it. The engine
    # reads `REIMAGINA_UNIT_CONCURRENCY` when `dbos_app.launch()` registers the
    # queue (default 2); until this line existed, `--slots 4` printed "slots=4",
    # divided `efficiency` by 4, and ran on 2 — so every artifact carrying
    # `"slots": 4` without the variable also set was mislabelled, and its
    # efficiency was understated by exactly the ratio of the two.
    # `tests/workloads/conftest.py:196` already does this; this file did not.
    #
    # Before `launch()`, which is called lazily at the first submit — so setting
    # it here, ahead of any orchestrator call, is what makes it take effect.
    os.environ["REIMAGINA_UNIT_CONCURRENCY"] = str(args.slots)

    removed = sweep_bench_rows(dsn)
    print(f"swept {removed} leftover benchmark rows; table vacuumed and analyzed")

    from reimagina.db import connect

    conn = connect(dsn)
    features = block_ids(conn, args.scope or None)
    if not features:
        parser.error("no block features found — is this a populated database?")
    groups = sorted(set(GROUP_OF.values()))
    heavy = sorted(s for s, c in CLASS_OF.items() if c == "heavy")
    print(
        f"scope: {len(features)} blocks, {len(EDGES)} indicators "
        f"in {len(groups)} dispatch groups ({len(heavy)} heavy), slots={args.slots}"
    )
    print(f"  triggered by: {', '.join(sorted(CHANGED_ATTRIBUTES))}")

    doc: dict = {
        "scope_features": len(features),
        "slots": args.slots,
        "reps": args.reps,
        "warm": args.reps > 1,
        "changed_attributes": sorted(CHANGED_ATTRIBUTES),
        "indicators": len(EDGES),
        "groups": groups,
        "heavy_indicators": heavy,
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
            last_per_indicator = result["per_indicator"]
        # The first repetition pays one-time costs — a cold worker, a freshly
        # declared workflow, a cold page cache — so it is recorded and excluded.
        doc["modes"][mode] = aggregate(reps[1:] if len(reps) > 1 else reps)
        doc["modes"][mode]["warmup"] = reps[0] if len(reps) > 1 else None
        doc["modes"][mode]["per_indicator"] = last_per_indicator

        # Which indicator actually cost the run, from the final repetition.
        if last_per_indicator:
            print(f"  {mode:8} slowest indicators:")
            for slug, info in sorted(
                last_per_indicator.items(), key=lambda kv: -kv[1]["ms"]
            )[:5]:
                mark = "HEAVY" if info["class"] == "heavy" else "     "
                print(f"    {mark}  {info['ms']:9.0f}ms  {slug}")

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
