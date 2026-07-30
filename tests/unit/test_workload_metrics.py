"""The workload suite's arithmetic — the part a silent error would poison.

Every conclusion in the ops-plane comparison is a number this module computed,
so these tests pin the definitions rather than the plumbing. They build
`UnitRecord`s by hand instead of running an engine: the question here is whether
`slr` and `head_of_line_ms` say what we claim they say, and an engine in the
loop would only make a wrong answer harder to see.

The two schedules below are the whole point of the suite — the same DAG, laid
out the way a wave scheduler runs it and the way a per-task scheduler runs it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reimagina.perf.workload_report import (
    UnitRecord,
    aggregate,
    compare_workload_reports,
    measure,
    ordering_violations,
)

T0 = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _at(start_s: float, dur_s: float) -> tuple[datetime, datetime]:
    return T0 + timedelta(seconds=start_s), T0 + timedelta(seconds=start_s + dur_s)


def _rec(slug: str, start_s: float, dur_s: float, status: str = "succeeded"):
    started, completed = _at(start_s, dur_s)
    return UnitRecord(
        slug=slug, status=status, started_at=started, completed_at=completed
    )


# A slow root with no dependents, beside an independent three-hop chain.
# Costs: slow=1.5, each hop=0.2.
EDGES = {
    "slow_root": (),
    "hop_a": (),
    "hop_b": ("hop_a",),
    "hop_c": ("hop_b",),
}

# Per-task: the chain runs alongside the slow root. Makespan 1.5.
PERTASK = [
    _rec("slow_root", 0.0, 1.5),
    _rec("hop_a", 0.0, 0.2),
    _rec("hop_b", 0.2, 0.2),
    _rec("hop_c", 0.4, 0.2),
]

# Wave: hop_b cannot start until the whole first wave — including slow_root —
# has finished, so the chain is dragged out behind it. Makespan 1.9.
WAVE = [
    _rec("slow_root", 0.0, 1.5),
    _rec("hop_a", 0.0, 0.2),
    _rec("hop_b", 1.5, 0.2),
    _rec("hop_c", 1.7, 0.2),
]


def _measure(records, makespan_s, **kw):
    return measure(
        records=records,
        edges=EDGES,
        t_submit=T0,
        t_terminal=T0 + timedelta(seconds=makespan_s),
        slots=4,
        **kw,
    )


# ── The claim the whole suite rests on ──────────────────────────────────────


def test_slr_separates_the_two_schedules():
    """Same DAG, same unit costs, same critical path — different schedule length.

    The critical path is identical (1.5s of slow_root, since the chain is only
    0.6s), so any difference in SLR is the scheduler and nothing else.
    """
    pertask = _measure(PERTASK, 1.5)
    wave = _measure(WAVE, 1.9)

    assert pertask["critical_path_ms"] == wave["critical_path_ms"] == 1500.0
    assert pertask["slr"] == 1.0
    assert wave["slr"] == pytest.approx(1.267, abs=0.001)


def test_head_of_line_names_why_slr_differs():
    """SLR says the wave schedule is worse; this says where the time went.

    hop_b was runnable at t=0.2 (hop_a done) and started at t=1.5 — a 1.3s wait
    on a sibling it does not depend on.
    """
    assert _measure(PERTASK, 1.5)["head_of_line_ms"] == 0.0
    assert _measure(WAVE, 1.9)["head_of_line_ms"] == pytest.approx(1300.0)


def test_head_of_line_waits_for_the_LAST_parent_not_the_first():
    """A unit with several parents is runnable when the slowest one finishes.

    ⚠ This test exists because a mutant that used `min(ready)` instead of
    `max(ready)` survived the whole rest of this file: every other node here has
    exactly one parent, and in the one multi-parent case the wrong answer was
    masked by a larger delay elsewhere. `lattice` and `layered_random` are mostly
    multi-parent nodes, so `min` would have reported a fat head-of-line delay for
    a scheduler that did nothing wrong — inventing the suite's headline finding.
    """
    edges = {"early": (), "late": (), "joiner": ("early", "late")}
    records = [
        _rec("early", 0.0, 0.5),
        _rec("late", 0.0, 1.5),
        _rec("joiner", 1.5, 0.1),  # started the instant `late` finished
    ]
    metrics = measure(
        records=records,
        edges=edges,
        t_submit=T0,
        t_terminal=T0 + timedelta(seconds=1.6),
        slots=4,
    )
    assert metrics["head_of_line_ms"] == 0.0


def test_a_converging_dag_hides_the_barrier_in_makespan():
    """Why the suite keeps both `barrier_probe` and `lattice`.

    Add a sink that depends on both branches and the slow root gates the
    makespan either way — SLR goes quiet while head-of-line still reports the
    delay. A suite that measured only makespan would conclude the wave barrier
    is free, which is true only for DAGs that re-converge.
    """
    edges = {**EDGES, "sink": ("slow_root", "hop_c")}
    pertask = [*PERTASK, _rec("sink", 1.5, 0.1)]
    wave = [*WAVE, _rec("sink", 1.9, 0.1)]

    m_pertask = measure(
        records=pertask,
        edges=edges,
        t_submit=T0,
        t_terminal=T0 + timedelta(seconds=1.6),
        slots=4,
    )
    m_wave = measure(
        records=wave,
        edges=edges,
        t_submit=T0,
        t_terminal=T0 + timedelta(seconds=2.0),
        slots=4,
    )
    # Both near the (now longer) critical path...
    assert m_pertask["slr"] == pytest.approx(1.0, abs=0.01)
    assert m_wave["slr"] == pytest.approx(1.25, abs=0.01)
    # ...but head-of-line still reports the wait that makespan absorbed.
    assert m_wave["head_of_line_ms"] == pytest.approx(1300.0)


# ── The correctness invariant ───────────────────────────────────────────────


def test_ordering_violation_is_detected():
    """A child that started before its parent finished is a real defect."""
    broken = [
        _rec("hop_a", 0.0, 0.5),
        _rec("hop_b", 0.2, 0.2),  # parent hop_a is not done until 0.5
    ]
    violations = ordering_violations(broken, EDGES)
    assert len(violations) == 1
    assert "hop_b started 300.0ms before parent hop_a" in violations[0]


def test_a_valid_schedule_has_no_violations():
    assert ordering_violations(PERTASK, EDGES) == []
    assert ordering_violations(WAVE, EDGES) == []


# ── Metrics that are omitted rather than guessed ────────────────────────────


def test_speedup_needs_a_sync_baseline_and_is_absent_without_one():
    """`sync` is T1. Without it, speedup and efficiency are not reported at all.

    They are omitted rather than defaulted because a plausible-looking 1.0 would
    silently claim the engine achieved no parallelism.
    """
    assert "speedup" not in _measure(PERTASK, 1.5)
    with_baseline = _measure(PERTASK, 1.5, sync_makespan_ms=2100.0)
    assert with_baseline["speedup"] == pytest.approx(1.4, abs=0.001)
    assert with_baseline["efficiency"] == pytest.approx(0.35, abs=0.001)


def test_observed_parallelism_reports_slots_actually_used():
    """busy / span: 2.1s of work inside a 1.5s window is 1.4 units of overlap."""
    assert _measure(PERTASK, 1.5)["observed_parallelism"] == pytest.approx(
        1.4, abs=0.01
    )
    # The wave schedule spreads the same work over a longer window.
    assert _measure(WAVE, 1.9)["observed_parallelism"] == pytest.approx(1.105, abs=0.01)


def test_a_run_with_no_timed_units_does_not_divide_by_zero():
    """Everything skipped or cancelled is a legal outcome, not a crash."""
    empty = measure(
        records=[],
        edges=EDGES,
        t_submit=T0,
        t_terminal=T0 + timedelta(seconds=0.4),
        slots=4,
    )
    assert empty["slr"] is None
    assert empty["observed_parallelism"] is None
    assert empty["units_timed"] == 0


# ── Aggregation and comparison ──────────────────────────────────────────────


def test_aggregate_takes_the_median_not_the_mean():
    """One slow repetition must not move the headline number."""
    reps = [{"slr": 1.0}, {"slr": 1.1}, {"slr": 9.0}]
    agg = aggregate(reps)
    assert agg["slr"] == 1.1
    assert agg["slr_min"] == 1.0
    assert agg["slr_max"] == 9.0
    assert agg["reps_measured"] == 3


def test_comparing_reports_from_different_machines_warns():
    """Silently diffing incomparable reports is the failure mode to prevent."""
    a = {"track": "dbos", "provenance": {"cpu_count": 10, "slots": 4}, "workloads": {}}
    b = {
        "track": "hatchet",
        "provenance": {"cpu_count": 4, "slots": 4},
        "workloads": {},
    }
    warnings = compare_workload_reports(a, b)["warnings"]
    assert any("cpu_count" in w for w in warnings)


def test_an_unwarmed_single_rep_report_is_flagged():
    base = {"provenance": {}, "workloads": {}}
    warnings = compare_workload_reports({**base, "warm": False}, {**base})["warnings"]
    assert any("unwarmed" in w for w in warnings)
