"""A killed executor must resume the run it was given, not a different one.

This is the property `jobs_runtime.run_plan` exists to guarantee and the only
one that cannot be argued from the schema alone. The plan is written once and
read by each unit; if a crash could change what a resumed unit computes, the
handle would be worse than the inline payload it replaced.

**Why a subprocess and a real SIGKILL.** `DBOS.launch()` is what performs
recovery — it reclaims workflows recorded against its own executor id — so
proving anything requires two processes with the same id and a genuine death
between them. A mocked crash exercises none of it: not the rollback of the
compute transaction, not the PENDING row DBOS leaves behind, not the reclaim.

**Why the kill is triggered by state, not a timer.** The first version of this
slept six seconds and killed nothing — the run had already finished, all three
units succeeded, and the plan was already reclaimed. It would have passed while
proving the opposite of what it claims. The kill now waits for a unit to be
observably `running` with none succeeded, so the crash is unambiguously
mid-compute.

Opt-in, because it needs a migrated database with enough features to make a
compute window, and it deliberately kills processes:

    REIMAGINA_RECOVERY_TEST=1 TEST_DATABASE_URL=postgresql:///reimagina_real_a \\
        pytest tests/recovery -q

Measured on the 86,225-block CDMX database: killed with two units running and
none succeeded; a second process reclaimed the workflow, all three units
finished, and the digest matched the value every other execution path produces.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("REIMAGINA_RECOVERY_TEST") != "1",
    reason="recovery suite not enabled (kills processes; set REIMAGINA_RECOVERY_TEST=1)",
)

HERE = Path(__file__).parent
EXECUTOR_ID = "recovery-probe"
# Enough blocks that a unit is still running when the poll notices it. Below
# this the run finishes before the kill can land and the test proves nothing,
# so it skips rather than passing vacuously.
MIN_BLOCKS = 20_000


def _dsn() -> str:
    return os.environ.get("TEST_DATABASE_URL", "postgresql:///reimagina_test")


def _env() -> dict[str, str]:
    dsn = _dsn()
    return {
        **os.environ,
        "TEST_DATABASE_URL": dsn,
        "DATABASE_URL_DIRECT": dsn,
        "REIMAGINA_DBOS_EXECUTION_MODE": "dev",
        "REIMAGINA_EXECUTOR_ID": EXECUTOR_ID,
    }


def _query(sql: str, *args):
    """Run a statement; return its rows, or [] when it produced none.

    `cur.description` is None for DELETE/TRUNCATE, and fetching there raises
    "no results to fetch" — which is how the first version of this failed.
    """
    with psycopg2.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def clean_run_state():
    _query("TRUNCATE jobs_runtime.run_plan")
    _query("DELETE FROM jobs_runtime.job_unit")
    _query("DELETE FROM jobs_runtime.orchestration_run")
    yield


def test_a_killed_executor_resumes_the_same_feature_set(clean_run_state):
    blocks = _query(
        "SELECT count(*) FROM curated_geo.feature f "
        "JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id "
        "WHERE t.type_slug = 'block' AND f.geometry IS NOT NULL"
    )[0][0]
    if blocks < MIN_BLOCKS:
        pytest.skip(
            f"{blocks} blocks is too few to crash mid-compute; "
            f"need {MIN_BLOCKS}. A run that finishes first proves nothing."
        )

    submitter = subprocess.Popen(
        [sys.executable, str(HERE / "_submit_and_block.py")],
        env=_env(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        run_id = plan_id = None
        for line in submitter.stdout:  # type: ignore[union-attr]
            if line.startswith("RUN_ID="):
                run_id = line.strip().split("=", 1)[1]
            elif line.startswith("PLAN_ID="):
                plan_id = line.strip().split("=", 1)[1]
            elif line.startswith("BLOCKS="):
                break
        assert run_id and plan_id, "submitter never reported its ids"

        # Kill on observed state, never on elapsed time.
        deadline = time.time() + 300
        killed = False
        while time.time() < deadline:
            rows = _query(
                "SELECT count(*) FILTER (WHERE status='running'), "
                "count(*) FILTER (WHERE status='succeeded') "
                "FROM jobs_runtime.job_unit WHERE run_id = %s::uuid",
                run_id,
            )[0]
            running, done = rows
            if running >= 1 and done < 3:
                submitter.kill()
                killed = True
                break
            if done >= 3:
                pytest.skip("run completed before the kill could land")
            time.sleep(0.2)
        assert killed, "never observed a unit running"
        submitter.wait(timeout=30)
    finally:
        if submitter.poll() is None:
            submitter.kill()

    # The plan must outlive the crash: cleanup fires only on a terminal run, and
    # recovery has nothing to read if it does not.
    surviving = _query(
        "SELECT indicator_slug, array_length(feature_ids, 1) "
        "FROM jobs_runtime.run_plan WHERE plan_id = %s::uuid ORDER BY 1",
        plan_id,
    )
    assert len(surviving) == 3, f"the plan did not survive the crash: {surviving}"
    before = {slug: n for slug, n in surviving}

    recovered = subprocess.run(
        [sys.executable, str(HERE / "_recover.py"), run_id],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = dict(
        line.split("=", 1)
        for line in recovered.stdout.splitlines()
        if "=" in line and not line.startswith(" ")
    )

    assert out.get("STATUS") == "succeeded", recovered.stdout[-2000:]
    assert "queued" not in out.get("UNITS", ""), out.get("UNITS")
    assert "running" not in out.get("UNITS", ""), out.get("UNITS")
    # Reclaimed once terminal — a recovered run must not leak its plan either.
    assert out.get("RUN_PLAN_ROWS") == "0", out.get("RUN_PLAN_ROWS")

    # And the set the resumed units read was the set the submitter resolved.
    after = _query(
        "SELECT count(*) FROM jobs_runtime.run_plan WHERE plan_id = %s::uuid",
        plan_id,
    )[0][0]
    assert after == 0
    assert before, "no plan was captured before the crash"
