"""Submit a full-city run and execute it in-process, then block until killed.

Half of the recovery experiment: this process is SIGKILLed partway through so a
second one can reclaim the workflow. It prints the run id and plan id first so
the harness can check both across the crash.
"""

from __future__ import annotations

import os
import time

import psycopg2

from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator

BLOCKS = """
SELECT f.id::text FROM curated_geo.feature f
JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id
WHERE t.type_slug = 'block' AND f.geometry IS NOT NULL ORDER BY f.id
"""


def main() -> int:
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(BLOCKS)
        blocks = [r[0] for r in cur.fetchall()]

    handle = DBOSOrchestrator().submit_run(
        conn,
        changed_attributes={"geometry"},
        edited_features=set(blocks),
        code_version="recovery-probe",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT plan_id::text FROM jobs_runtime.run_plan LIMIT 1")
        row = cur.fetchone()
    print(f"RUN_ID={handle.run_id}", flush=True)
    print(f"PLAN_ID={row[0] if row else 'NONE'}", flush=True)
    print(f"BLOCKS={len(blocks)}", flush=True)

    # dev mode self-executes; hold the process open so the kill lands mid-run.
    while True:
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
