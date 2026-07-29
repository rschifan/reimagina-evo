"""Turn a workload run into scheduling metrics, and two reports into a diff.

**This module must stay byte-identical on both ops-plane worktrees.** It is the
measuring instrument; if it drifts, the two reports stop being comparable and
nothing downstream will say so.

Everything is derived from ``jobs_runtime.job_run`` plus two timestamps taken
from the *Postgres* clock — deliberately not Python's. A unit's `started_at` is
a server timestamp, so taking `t_submit` from the same server is what makes
`dispatch_lead_ms` and the parent/child ordering check exact rather than
approximately right. (That those columns carry real values at all is recent:
`start_job`/`complete_job` used `now()`, which is `transaction_timestamp()` and
therefore constant across a unit's single transaction, so every duration ever
recorded was 00:00:00.)

The metric names are the scheduling literature's, not ours. **Schedule Length
Ratio** — makespan over critical path — is the headline, because it is
dimensionless and >= 1, so it compares across DAG shapes and machines in a way
raw milliseconds cannot.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# ── Reading a run back out of the database ──────────────────────────────────

_UNIT_SQL = """
SELECT jr.id::text        AS job_run_id,
       jr.model_family    AS model_family,
       jr.status          AS status,
       jr.started_at      AS started_at,
       jr.completed_at    AS completed_at,
       (SELECT i.slug
          FROM curated_geo.indicator_result ir
          JOIN curated_geo.indicator_catalog i ON i.id = ir.indicator_id
         WHERE ir.job_run_id = jr.id
         LIMIT 1)         AS result_slug
  FROM jobs_runtime.job_run jr
 WHERE jr.run_id = %s
 ORDER BY jr.started_at
"""
"""⚠ Two ways to name a unit, because `job_run` has no indicator column.

A synthetic workload puts its slug in `model_family` (it has no FK and no CHECK,
so this is free). A real indicator puts its *family* there — `urban_form` for
three different indicators — so those are told apart by joining through the
`indicator_result` rows they wrote. Preferring the join and falling back to
`model_family` covers both, and covers a real composite that legitimately wrote
zero rows.
"""


@dataclass(frozen=True)
class UnitRecord:
    slug: str
    status: str
    started_at: datetime
    completed_at: datetime | None

    @property
    def duration_ms(self) -> float:
        if self.completed_at is None:
            return 0.0
        return (self.completed_at - self.started_at).total_seconds() * 1000.0


def db_now(conn) -> datetime:
    """The Postgres clock. Every timestamp in a report comes from here."""
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def unit_records(conn, run_id: str) -> list[UnitRecord]:
    with conn.cursor() as cur:
        cur.execute(_UNIT_SQL, (run_id,))
        rows = cur.fetchall()
    out: list[UnitRecord] = []
    for job_run_id, family, status, started, completed, result_slug in rows:
        if started is None:
            # Never dispatched. It has no timing to contribute and the port's
            # own unit statuses already report it.
            continue
        out.append(
            UnitRecord(
                slug=result_slug or family or job_run_id,
                status=status,
                started_at=started,
                completed_at=completed,
            )
        )
    return out


# ── Metrics ─────────────────────────────────────────────────────────────────


def ordering_violations(
    records: Sequence[UnitRecord], edges: Mapping[str, Sequence[str]]
) -> list[str]:
    """Children that started before a parent finished.

    The one hard correctness invariant a scheduler cannot get wrong whatever its
    strategy. No tolerance is applied: every timestamp comes from one Postgres
    clock, so strict ordering genuinely holds and a violation is real.
    """
    finished = {r.slug: r.completed_at for r in records if r.completed_at}
    started = {r.slug: r.started_at for r in records}
    bad: list[str] = []
    for slug, deps in edges.items():
        if slug not in started:
            continue
        for dep in deps:
            done = finished.get(dep)
            if done is not None and started[slug] < done:
                gap = (done - started[slug]).total_seconds() * 1000.0
                bad.append(f"{slug} started {gap:.1f}ms before parent {dep} finished")
    return bad


def _critical_path_ms(
    records: Sequence[UnitRecord], edges: Mapping[str, Sequence[str]]
) -> float:
    """Longest dependency path, weighted by *measured* durations.

    Measured rather than nominal on purpose: SLR then answers "how close did the
    scheduler get to this DAG's lower bound on the day", which stays meaningful
    even if the machine was slow. It also means makespans are comparable only
    within a track, while SLR is comparable across them.
    """
    duration = {r.slug: r.duration_ms for r in records}
    memo: dict[str, float] = {}

    def longest(slug: str) -> float:
        if slug not in memo:
            memo[slug] = duration.get(slug, 0.0) + max(
                [longest(d) for d in edges.get(slug, ())] or [0.0]
            )
        return memo[slug]

    return max((longest(s) for s in edges), default=0.0)


def _head_of_line_ms(
    records: Sequence[UnitRecord], edges: Mapping[str, Sequence[str]]
) -> float:
    """Worst delay between a unit being *able* to start and actually starting.

    Not a standard scheduling metric — the diagnostic that names *why* SLR
    differs. A wave scheduler holds a ready child until every sibling in its
    wave is done, and this is exactly that wait.
    """
    finished = {r.slug: r.completed_at for r in records if r.completed_at}
    started = {r.slug: r.started_at for r in records}
    worst = 0.0
    for slug, deps in edges.items():
        ready = [finished[d] for d in deps if d in finished]
        if not ready or slug not in started:
            continue
        worst = max(worst, (started[slug] - max(ready)).total_seconds() * 1000.0)
    return worst


def measure(
    *,
    records: Sequence[UnitRecord],
    edges: Mapping[str, Sequence[str]],
    t_submit: datetime,
    t_terminal: datetime,
    slots: int,
    sync_makespan_ms: float | None = None,
) -> dict[str, Any]:
    """One repetition's numbers. Every duration in milliseconds."""
    makespan = (t_terminal - t_submit).total_seconds() * 1000.0
    timed = [r for r in records if r.completed_at is not None]

    first_start = min((r.started_at for r in records), default=None)
    last_complete = max((r.completed_at for r in timed), default=None)

    busy = sum(r.duration_ms for r in timed)
    span = (
        (last_complete - first_start).total_seconds() * 1000.0
        if first_start and last_complete
        else 0.0
    )
    critical = _critical_path_ms(records, edges)

    out: dict[str, Any] = {
        "makespan_ms": round(makespan, 1),
        "critical_path_ms": round(critical, 1),
        "slr": round(makespan / critical, 3) if critical else None,
        "span_ms": round(span, 1),
        "busy_ms": round(busy, 1),
        "observed_parallelism": round(busy / span, 3) if span else None,
        "dispatch_lead_ms": round(
            (first_start - t_submit).total_seconds() * 1000.0, 1
        )
        if first_start
        else None,
        "settle_lag_ms": round(
            (t_terminal - last_complete).total_seconds() * 1000.0, 1
        )
        if last_complete
        else None,
        "head_of_line_ms": round(_head_of_line_ms(records, edges), 1),
        "units_timed": len(timed),
    }

    # Omitted rather than wrong. `sync` is T1 for speedup, so it has none of its
    # own; and where retry policy differs between an engine and `sync`, a
    # speedup number would compare retry policies while looking like it compared
    # schedulers.
    if sync_makespan_ms:
        speedup = sync_makespan_ms / makespan if makespan else None
        out["speedup"] = round(speedup, 3) if speedup else None
        out["efficiency"] = round(speedup / slots, 3) if speedup and slots else None
    return out


_AGGREGATED = (
    "makespan_ms",
    "critical_path_ms",
    "slr",
    "span_ms",
    "busy_ms",
    "observed_parallelism",
    "dispatch_lead_ms",
    "settle_lag_ms",
    "head_of_line_ms",
    "speedup",
    "efficiency",
)


def aggregate(reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Median with min/max across measured repetitions.

    Median rather than mean: one GC pause or one cold connection should not move
    the headline number, and with 2-3 measured reps a mean is exactly what it
    would move.
    """
    if not reps:
        return {}
    out: dict[str, Any] = {"reps_measured": len(reps)}
    for key in _AGGREGATED:
        vals = [r[key] for r in reps if r.get(key) is not None]
        if not vals:
            continue
        out[key] = round(statistics.median(vals), 3)
        if len(vals) > 1:
            out[f"{key}_min"] = round(min(vals), 3)
            out[f"{key}_max"] = round(max(vals), 3)
    for key in ("units_timed",):
        vals = [r[key] for r in reps if r.get(key) is not None]
        if vals:
            out[key] = max(vals)
    return out


# ── Comparing two tracks ────────────────────────────────────────────────────

_PROVENANCE_MUST_MATCH = ("cpu_count", "postgres_version", "slots", "scale", "reps")


def compare_workload_reports(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict:
    """Diff two track reports, refusing to be quiet about incomparable inputs."""
    warnings: list[str] = []
    pa, pb = a.get("provenance", {}), b.get("provenance", {})
    for key in _PROVENANCE_MUST_MATCH:
        if pa.get(key) != pb.get(key):
            warnings.append(
                f"{key}: {pa.get(key)!r} vs {pb.get(key)!r} — these reports are "
                "not directly comparable"
            )
    if not a.get("warm", True) or not b.get("warm", True):
        warnings.append(
            "at least one report is a single unwarmed repetition (reps=1); "
            "treat every number as indicative only"
        )

    rows: list[dict[str, Any]] = []
    ids = [w for w in a.get("workloads", {}) if w in b.get("workloads", {})]
    for wid in ids:
        wa, wb = a["workloads"][wid], b["workloads"][wid]
        for mode in sorted(set(wa.get("modes", {})) | set(wb.get("modes", {}))):
            rows.append(
                {
                    "workload": wid,
                    "mode": mode,
                    "predicted_wave_slr": wa.get("predicted", {}).get("wave_slr"),
                    "predicted_pertask_slr": wa.get("predicted", {}).get("pertask_slr"),
                    "a": wa.get("modes", {}).get(mode, {}),
                    "b": wb.get("modes", {}).get(mode, {}),
                }
            )
    return {
        "a_track": a.get("track"),
        "b_track": b.get("track"),
        "warnings": warnings,
        "rows": rows,
    }


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(comparison: Mapping[str, Any]) -> str:
    a, b = comparison["a_track"], comparison["b_track"]
    out: list[str] = [f"# Ops-plane workload comparison — {a} vs {b}", ""]

    if comparison["warnings"]:
        out += ["> **Read these first.**", ""]
        out += [f"> - {w}" for w in comparison["warnings"]] + [""]

    out += [
        "`slr` = makespan / critical path; 1.00 means the scheduler achieved the",
        "DAG's lower bound. `hol` = worst head-of-line delay, the wait between a",
        "unit becoming runnable and actually starting. `pred` columns are what an",
        "idealised wave / per-task scheduler would do on nominal costs.",
        "",
        f"| workload | mode | pred wave | pred pertask | slr {a} | slr {b} | hol {a} | hol {b} | makespan {a} | makespan {b} |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in comparison["rows"]:
        ra, rb = row["a"], row["b"]
        out.append(
            f"| `{row['workload']}` | {row['mode']} "
            f"| {_fmt(row['predicted_wave_slr'])} "
            f"| {_fmt(row['predicted_pertask_slr'])} "
            f"| {_fmt(ra.get('slr'))} | {_fmt(rb.get('slr'))} "
            f"| {_fmt(ra.get('head_of_line_ms'), 0)} | {_fmt(rb.get('head_of_line_ms'), 0)} "
            f"| {_fmt(ra.get('makespan_ms'), 0)} | {_fmt(rb.get('makespan_ms'), 0)} |"
        )

    out += [
        "",
        "## Parallelism and speedup",
        "",
        f"| workload | mode | par {a} | par {b} | speedup {a} | speedup {b} | eff {a} | eff {b} |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in comparison["rows"]:
        ra, rb = row["a"], row["b"]
        out.append(
            f"| `{row['workload']}` | {row['mode']} "
            f"| {_fmt(ra.get('observed_parallelism'))} | {_fmt(rb.get('observed_parallelism'))} "
            f"| {_fmt(ra.get('speedup'))} | {_fmt(rb.get('speedup'))} "
            f"| {_fmt(ra.get('efficiency'))} | {_fmt(rb.get('efficiency'))} |"
        )
    return "\n".join(out) + "\n"


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m reimagina.perf.workload_report A.json B.json",
            file=sys.stderr,
        )
        return 2
    with open(argv[0]) as fh_a, open(argv[1]) as fh_b:
        comparison = compare_workload_reports(json.load(fh_a), json.load(fh_b))
    print(render_markdown(comparison))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
