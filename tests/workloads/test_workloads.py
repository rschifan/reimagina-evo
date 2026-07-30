"""Run each workload on each implementation; assert little, record a lot.

The split is deliberate. What is **asserted** is what must hold however an engine
chooses to schedule — every unit reaches a terminal state, no child starts before
its parents finished, no unit's body completes twice, a failed unit does not take
its independent siblings with it. What is **recorded** is everything the two
tracks are allowed to differ on,
because the deliverable is a documented side-by-side and a difference there is
the finding rather than a failure.

The report lands in ``artifacts/workloads-<track>.json``; diff two of them with
``python -m reimagina.perf.workload_report a.json b.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reimagina.perf.workload_report import (
    aggregate,
    duplicate_executions,
    measure,
    ordering_violations,
)
from reimagina.perf.workloads import WORKLOADS, predictions

from .conftest import TRACK, SLOTS, Implementation, reps, run_once


@pytest.fixture(scope="session")
def report(provenance):
    """Accumulates every measurement, then writes the track's report once."""
    doc: dict = {
        "track": TRACK,
        "warm": reps() > 1,
        "provenance": provenance,
        "workloads": {},
    }
    yield doc

    _inject_speedup(doc, SLOTS)
    out = Path("artifacts") / f"workloads-{TRACK}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str))
    print(f"\nworkload report -> {out}")


def _inject_speedup(doc: dict, slots: int) -> None:
    """Speedup against the `sync` control, computed after every mode has run.

    Post-hoc rather than inside `measure` so it cannot depend on pytest's
    parametrisation order. Ratio of medians rather than median of ratios — one
    slow repetition then moves neither number.

    `partial_failure` is skipped on purpose: this track retries a failing unit
    and `sync` does not, so a speedup there would compare retry policies while
    looking like it compared schedulers.
    """
    for wid, entry in doc["workloads"].items():
        baseline = entry.get("modes", {}).get("sync", {}).get("makespan_ms")
        if not baseline or wid == "partial_failure":
            continue
        for mode, metrics in entry["modes"].items():
            if mode == "sync" or not metrics.get("makespan_ms"):
                continue
            speedup = baseline / metrics["makespan_ms"]
            metrics["speedup"] = round(speedup, 3)
            metrics["efficiency"] = round(speedup / slots, 3)


@pytest.mark.parametrize("workload", WORKLOADS, ids=lambda w: w.id)
def test_workload(workload, impl: Implementation, install, conn, report):
    orchestrator = install(workload, impl)
    measured: list[dict] = []
    total = reps()

    for rep in range(total):
        result = run_once(orchestrator, conn, workload, impl)

        # ── Asserted: true of any scheduler ─────────────────────────────────
        seen = {r.slug for r in result["records"]}
        reported = set(result["unit_statuses"])
        assert reported, f"{workload.id}/{impl.mode}: run reported no units at all"

        if not workload.expect_failure:
            missing = set(workload.slugs) - seen
            assert not missing, (
                f"{workload.id}/{impl.mode}: {sorted(missing)} never ran "
                f"(reported statuses: {result['unit_statuses']})"
            )

        violations = ordering_violations(result["records"], workload.edges)
        assert not violations, f"{workload.id}/{impl.mode}: {violations}"

        # `s_compute` sets `retries_allowed=False`, so a second completion here
        # is never a retry — it is recovery re-claiming work, which `_executor_id`
        # documents as a double execution on dbos 2.28.0. Asserted rather than
        # recorded because no engine may do it. Nothing else in this suite would
        # notice: `seen` above is a set and `ordering_violations` keys by slug, so
        # a unit that ran twice collapses into one record and passes silently.
        duplicates = duplicate_executions(result["records"])
        assert not duplicates, f"{workload.id}/{impl.mode}: {duplicates}"

        if workload.id == "partial_failure":
            _assert_failure_isolation(result, impl)

        # ── Recorded: where the tracks are allowed to differ ────────────────
        if rep == 0 and total > 1:
            continue  # warm-up: registration and connection setup, not scheduling
        measured.append(
            measure(
                records=result["records"],
                edges=workload.edges,
                t_submit=result["t_submit"],
                t_terminal=result["t_terminal"],
                slots=SLOTS,
            )
        )

    entry = report["workloads"].setdefault(
        workload.id,
        {
            "description": workload.description,
            "notes": workload.notes,
            "nodes": len(workload.nodes),
            "predicted": predictions(workload, slots=SLOTS),
            "modes": {},
        },
    )
    entry["modes"][impl.mode] = aggregate(measured)


def _assert_failure_isolation(result: dict, impl: Implementation) -> None:
    """One unit failing must not take its independent siblings with it.

    The *vocabulary* is left to the engine — a dependent of a failed unit is
    `skipped` where we own the tables and `cancelled` where the engine does — so
    this asserts the shape (it did not succeed) rather than the word.
    """
    statuses = result["unit_statuses"]
    assert statuses.get("flood_risk_zonal") == "failed", statuses
    for sibling in ("tree_canopy_share", "heat_island_delta"):
        assert statuses.get(sibling) == "succeeded", (
            f"{impl.mode}: independent sibling {sibling} did not survive a "
            f"failure elsewhere in the DAG: {statuses}"
        )
    dependent = statuses.get("resilience_index")
    assert dependent != "succeeded", (
        f"{impl.mode}: resilience_index depends on a failed unit and must not "
        f"report success, got {dependent!r}"
    )


@pytest.mark.skipif(
    os.environ.get("REIMAGINA_BENCH") != "1", reason="workload suite not enabled"
)
def test_predictions_are_self_consistent():
    """The simulator must agree with itself before it is used as a yardstick.

    A per-task schedule can never be longer than the wave schedule of the same
    DAG — it has strictly fewer ordering constraints — and neither can beat the
    critical path. If this fails, every `predicted_*` column in the report is
    noise.
    """
    for workload in WORKLOADS:
        p = predictions(workload, slots=SLOTS)
        assert p["pertask_makespan_s"] <= p["wave_makespan_s"] + 1e-9, workload.id
        assert p["pertask_makespan_s"] >= p["critical_path_s"] - 1e-9, workload.id
        assert p["wave_slr"] >= 1.0 - 1e-9, workload.id
