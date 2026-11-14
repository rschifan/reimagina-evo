"""Second half of the cross-executor experiment: a *different* executor sweeps.

``_recover.py`` relaunches with the same ``executor_id``, which is how DBOS
reclaims its own work at ``launch()``. This process deliberately uses a
different one, so launching recovers nothing, and the only thing that can
finish the run is ``orchestration.reclaim.sweep`` deciding the dead executor
is dead and re-enqueuing its workflows here.
"""

from __future__ import annotations

import os
import sys
import time

import psycopg2

# Run as a script, not imported as a module, so its own directory is not
# already importable under every launcher. Inserted before the import below
# rather than in ``__main__``, which runs too late.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _recover import digest  # noqa: E402


def main() -> int:
    run_id = sys.argv[1]
    dead_executor = sys.argv[2]

    from reimagina.orchestration import dbos_app
    from reimagina.orchestration.reclaim import sweep

    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    conn.autocommit = True

    dbos_app.launch()
    print(f"LAUNCHED_AS={dbos_app._executor_id()}", flush=True)

    # Nothing of ours was recovered at launch: the ids differ on purpose.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM jobs_runtime.job_unit "
            "WHERE run_id = %s::uuid AND status IN ('queued', 'running')",
            (run_id,),
        )
        print(f"UNFINISHED_BEFORE_SWEEP={cur.fetchone()[0]}", flush=True)

    report = sweep()
    print(f"DEAD={list(report.dead_executors)}", flush=True)
    print(f"RECOVERED={report.workflows_recovered}", flush=True)
    print(f"ERRORS={list(report.errors)}", flush=True)
    print(f"SWEPT_THE_DEAD_ONE={dead_executor in report.dead_executors}", flush=True)

    deadline = time.time() + 600
    status = None
    while time.time() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM jobs_runtime.orchestration_run WHERE id = %s::uuid",
                (run_id,),
            )
            row = cur.fetchone()
        status = row[0] if row else None
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            break
        time.sleep(1.0)

    sha, rows = digest(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indicator_slug, status FROM jobs_runtime.job_unit "
            "WHERE run_id = %s::uuid ORDER BY indicator_slug",
            (run_id,),
        )
        units = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM jobs_runtime.ops_executor_heartbeat "
            "WHERE executor_id = %s",
            (dead_executor,),
        )
        remaining = cur.fetchone()[0]

    print(f"STATUS={status}", flush=True)
    print(f"UNITS={units}", flush=True)
    print(f"DIGEST={sha[:16]} ROWS={rows}", flush=True)
    print(f"DEAD_HEARTBEAT_ROWS={remaining}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
