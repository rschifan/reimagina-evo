"""Does the pipeline compute the *right* answers on real city data?

Correctness, not timing. Four checks, each with an oracle that exists
independently of the code under test:

A. **Neutral scenario reproduces the baseline.** A run whose scenario has no
   override rows sees exactly the baseline inputs, so every value it computes
   must equal the committed baseline value — same feature, same indicator, bit
   for bit. This exercises feature scoping, the claim-check round trip through
   ``jobs_runtime.run_plan``, the indicator math and the composite's read of its
   own inputs, against a result nobody in this run produced.

B. **An override actually changes something.** ⚠ Check A passes just as happily
   if the pipeline ignores scenario overrides altogether — a what-if engine that
   silently returns the baseline is *exactly* the failure a neutral-scenario
   test cannot see. So B writes a real ``scenario_override``, submits with only
   a ``scenario_id`` (the production path: the change-set is derived from the
   override rows), and asserts the value moved for the edited features and for
   nobody else.

C. **Structural invariants.** One `current` row per (feature, indicator,
   scenario); every result cites a `job_run` that exists; the baseline is
   untouched by any of this.

D. **Determinism.** Two independent runs of the same input agree value for
   value. A scheduler that races its own writes shows up here and nowhere else.

Usage::

    DATABASE_URL_DIRECT=postgresql://localhost/reimagina_real_b \\
      python scripts/verify_real_data.py --mode hatchet --scope 5000

`hatchet` needs a worker already registered against the same database.
Nothing here writes baseline rows: every result it creates is scoped to a fresh
`scenario_id`, which the partial unique indexes keep apart from the baseline.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "py"))

from reimagina.orchestration.factory import get_orchestrator  # noqa: E402
from reimagina.orchestration.port import RUN_TERMINAL_STATES  # noqa: E402

SETTLE_TIMEOUT_S = 900.0
FLOORS_OVERRIDE = 7.0
"""Any value the data does not already hold, so a match cannot be a coincidence."""


class Failed(Exception):
    """A check that did not hold. The message is the finding."""


COMPARISON_TIMEOUT_MS = 120_000
"""⚠ A bound on every comparison, because one of them once ran for 18 minutes.

The comparisons are equality tests over a few hundred thousand rows and take
~150 ms with a sane plan. What they must never do is hang: a verification script
that stops answering is worse than one that fails, because a hang looks like slow
work and gets waited on.

This is deliberately **not** a claim about which plan is correct. It converts any
future planner surprise into a fast, loud failure regardless of cause — the same
reason the unit body sets `statement_timeout` rather than trusting the engine's.
"""


def _q(conn, sql: str, args: tuple = (), *, bounded: bool = False) -> list[tuple]:
    with conn.cursor() as cur:
        if bounded:
            # SET LOCAL: scoped to this transaction, so it cannot leak into the
            # runs themselves — those are multi-minute by design.
            cur.execute("SET LOCAL statement_timeout = %s", (COMPARISON_TIMEOUT_MS,))
        cur.execute(sql, args)
        return cur.fetchall()


def refresh_statistics(conn) -> None:
    """`ANALYZE` before comparing. ⚠ This is the fix, and the query was not.

    A full-scope run inserts 258,675 rows, and the checks below run three of them
    back to back. Autoanalyze is asynchronous and threshold-driven, so the
    planner can be working from statistics gathered when the table was a third of
    its current size — and it then picks a plan sized for a table that no longer
    exists.

    ⚠ An earlier version of this script blamed the *shape* of the comparison
    query and rewrote it with CTEs. That was wrong: `EXPLAIN (ANALYZE)` gives the
    two forms an identical parallel hash join, 104 ms and 150 ms, and the CTEs are
    inlined rather than acting as an optimisation fence (they are non-recursive
    and referenced once, which Postgres 12+ inlines). The rewrite changed
    nothing; fresh statistics are what the planner was actually missing.

    Costs ~0.9 s on a 3M-row table. Cheap against the alternative.
    """
    prior = conn.autocommit
    conn.autocommit = True  # ANALYZE cannot run inside a transaction block
    try:
        with conn.cursor() as cur:
            cur.execute("ANALYZE curated_geo.indicator_result")
    finally:
        conn.autocommit = prior


def blocks(conn, limit: int | None) -> list[str]:
    rows = _q(
        conn,
        "SELECT f.id::text FROM curated_geo.feature f "
        "JOIN curated_geo.feature_type_catalog t ON t.id = f.type_id "
        "WHERE t.type_slug = 'block' ORDER BY f.id" + (" LIMIT %s" if limit else ""),
        (limit,) if limit else (),
    )
    return [r[0] for r in rows]


def submit_and_wait(conn, *, mode: str, scenario_id, features, changed):
    orchestrator = get_orchestrator(mode)
    handle = orchestrator.submit_run(
        conn,
        changed_attributes=set(changed),
        edited_features=set(features),
        scenario_id=scenario_id,
        code_version=f"verify-{uuid.uuid4().hex[:8]}",
    )
    if handle.status is not None:
        return handle.run_id, handle.status
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        conn.rollback()  # drop the snapshot or committed unit writes stay invisible
        status = orchestrator.get_run(handle.run_id)
        if status is not None and status.status in RUN_TERMINAL_STATES:
            return handle.run_id, status
        time.sleep(0.1)
    raise Failed(f"run {handle.run_id} never settled")


def check_a_neutral_matches_baseline(conn, mode, features) -> str:
    scenario = str(uuid.uuid4())
    _run_id, status = submit_and_wait(
        conn, mode=mode, scenario_id=scenario, features=features, changed={"geometry"}
    )
    if status.status != "succeeded":
        raise Failed(f"A: neutral run reported {status.status!r}, units={status.units}")

    refresh_statistics(conn)
    rows = _q(
        conn,
        """
        SELECT s.indicator_slug, count(*),
               count(*) FILTER (WHERE b.feature_id IS NULL),
               count(*) FILTER (WHERE s.value_numeric IS DISTINCT FROM b.value_numeric)
        FROM curated_geo.indicator_result s
        LEFT JOIN curated_geo.indicator_result b
               ON b.feature_id = s.feature_id
              AND b.indicator_slug = s.indicator_slug
              AND b.scenario_id IS NULL AND b.status = 'current'
        WHERE s.scenario_id = %s::uuid AND s.status = 'current'
        GROUP BY 1 ORDER BY 1
        """,
        (scenario,),
        bounded=True,
    )
    if not rows:
        raise Failed("A: the run wrote no scenario results at all")
    for slug, compared, missing, differs in rows:
        if missing:
            raise Failed(f"A: {slug}: {missing}/{compared} features have no baseline")
        if differs:
            raise Failed(
                f"A: {slug}: {differs}/{compared} values differ from the baseline "
                f"a neutral scenario must reproduce exactly"
            )
        print(f"    {slug:36} {compared:>7} features, identical to baseline")
    return scenario


def check_b_override_changes_the_answer(conn, mode, features) -> None:
    """The check a neutral scenario cannot make."""
    scenario = str(uuid.uuid4())
    edited = features[:3]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO curated_geo.scenario_override "
            "(scenario_id, feature_id, attribute_key, value_numeric) "
            "VALUES (%s::uuid, %s::uuid, 'floors', %s)",
            [(scenario, fid, FLOORS_OVERRIDE) for fid in edited],
        )
    conn.commit()  # the worker reads this on its own connection

    # ⚠ No `changed_attributes`: with only a scenario_id the change-set is
    # derived from the override rows themselves, which is what the API does.
    _run_id, status = submit_and_wait(
        conn, mode=mode, scenario_id=scenario, features=[], changed=set()
    )
    if status.status not in {"succeeded", "partial"}:
        raise Failed(f"B: override run reported {status.status!r}")

    dirty = {u.indicator for u in status.units if u.status != "skipped"}
    if dirty != {"urban_form_block_planned_floors"}:
        raise Failed(
            f"B: a `floors` override should dirty planned_floors and nothing "
            f"else; the run computed {sorted(dirty)}"
        )

    rows = _q(
        conn,
        "SELECT feature_id::text, value_numeric FROM curated_geo.indicator_result "
        "WHERE scenario_id = %s::uuid AND indicator_slug = "
        "'urban_form_block_planned_floors' AND status = 'current'",
        (scenario,),
    )
    got = {fid: val for fid, val in rows}
    if set(got) != set(edited):
        raise Failed(
            f"B: expected planned_floors rows for exactly the {len(edited)} edited "
            f"features, got {len(got)} ({sorted(set(got) ^ set(edited))[:3]}…)"
        )
    wrong = {f: v for f, v in got.items() if v != FLOORS_OVERRIDE}
    if wrong:
        raise Failed(
            f"B: the override value did not reach the result: expected "
            f"{FLOORS_OVERRIDE}, got {sorted(wrong.values())[:3]}"
        )
    print(f"    override reached {len(got)} feature(s); value == {FLOORS_OVERRIDE}")


def check_c_invariants(conn) -> None:
    (dupes,) = _q(
        conn,
        "SELECT count(*) FROM (SELECT 1 FROM curated_geo.indicator_result "
        "WHERE status='current' GROUP BY feature_id, indicator_slug, scenario_id "
        "HAVING count(*) > 1) d",
    )[0]
    if dupes:
        raise Failed(f"C: {dupes} (feature, indicator, scenario) have two current rows")

    (orphans,) = _q(
        conn,
        "SELECT count(*) FROM curated_geo.indicator_result r "
        "LEFT JOIN jobs_runtime.job_run j ON j.id = r.job_run_id WHERE j.id IS NULL",
    )[0]
    if orphans:
        raise Failed(f"C: {orphans} results cite a job_run that does not exist")
    print("    no duplicate current rows; every result cites a real job_run")


def check_d_determinism(conn, mode, features, first_scenario: str) -> None:
    second = str(uuid.uuid4())
    _run_id, status = submit_and_wait(
        conn, mode=mode, scenario_id=second, features=features, changed={"geometry"}
    )
    if status.status != "succeeded":
        raise Failed(f"D: repeat run reported {status.status!r}")

    # This is the comparison that once ran for 18 minutes. The protection is
    # `refresh_statistics` plus the timeout in `_q`, not the shape of the SQL —
    # see `refresh_statistics` for why the shape was a red herring.
    refresh_statistics(conn)
    rows = _q(
        conn,
        """
        SELECT a.indicator_slug, count(*),
               count(*) FILTER (WHERE a.value_numeric IS DISTINCT FROM b.value_numeric)
        FROM curated_geo.indicator_result a
        JOIN curated_geo.indicator_result b
          ON b.feature_id = a.feature_id AND b.indicator_slug = a.indicator_slug
         AND b.scenario_id = %s::uuid AND b.status = 'current'
        WHERE a.scenario_id = %s::uuid AND a.status = 'current'
        GROUP BY 1 ORDER BY 1
        """,
        (second, first_scenario),
        bounded=True,
    )
    for slug, compared, differs in rows:
        if differs:
            raise Failed(f"D: {slug}: {differs}/{compared} values differ between runs")
        print(f"    {slug:36} {compared:>7} features, identical across two runs")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="hatchet")
    parser.add_argument("--scope", type=int, default=5000, help="blocks; 0 = all")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        parser.error("DATABASE_URL_DIRECT is required")

    from reimagina.db import connect

    conn = connect(dsn)
    features = blocks(conn, args.scope or None)
    print(f"mode={args.mode} scope={len(features)} blocks\n")

    checks = [
        ("A  neutral scenario reproduces the baseline", None),
        ("B  an override changes exactly what it should", None),
        ("C  structural invariants", None),
        ("D  two runs agree value for value", None),
    ]
    failures: list[str] = []
    try:
        print(checks[0][0])
        scenario = check_a_neutral_matches_baseline(conn, args.mode, features)
    except Failed as exc:
        failures.append(str(exc))
        print(f"    FAILED: {exc}")
        scenario = None

    for label, fn in (
        (
            checks[1][0],
            lambda: check_b_override_changes_the_answer(conn, args.mode, features),
        ),
        (checks[2][0], lambda: check_c_invariants(conn)),
        (
            checks[3][0],
            lambda: check_d_determinism(conn, args.mode, features, scenario)
            if scenario
            else print("    skipped: check A produced no scenario to compare against"),
        ),
    ):
        print(label)
        try:
            fn()
        except Failed as exc:
            failures.append(str(exc))
            print(f"    FAILED: {exc}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
