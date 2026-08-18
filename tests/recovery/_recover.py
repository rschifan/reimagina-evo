"""Second half: launch DBOS with the same executor id and let recovery finish.

DBOS reclaims workflows recorded against its own executor id at ``launch()``.
The id must therefore be identical to the killed process's — the default is
``hostname:role``, which is stable across restarts by design.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

import psycopg2

DIGEST = """
SELECT ir.feature_id::text, i.slug, round(ir.value_numeric::numeric, 9)::text
FROM curated_geo.indicator_result ir
JOIN curated_geo.indicator_catalog i ON i.id = ir.indicator_id
WHERE ir.status = 'current' ORDER BY 1, 2
"""


def digest(conn) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with conn.cursor() as cur:
        cur.execute(DIGEST)
        for fid, slug, val in cur.fetchall():
            h.update(f"{fid}|{slug}|{val}\n".encode())
            n += 1
    return h.hexdigest(), n


def main() -> int:
    run_id = sys.argv[1]
    from reimagina.orchestration import dbos_app

    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    conn.autocommit = True

    dbos_app.launch()  # recovery happens here
    print("LAUNCHED", flush=True)

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
        cur.execute("SELECT count(*) FROM jobs_runtime.run_plan")
        plans = cur.fetchone()[0]

    print(f"STATUS={status}", flush=True)
    print(f"UNITS={units}", flush=True)
    print(f"DIGEST={sha[:16]} ROWS={rows}", flush=True)
    print(f"RUN_PLAN_ROWS={plans}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
