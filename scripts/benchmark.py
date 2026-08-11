"""The benchmark. One command, both halves, one report per track.

There were two benchmarks and no single answer. ``tests/workloads`` measured how
an engine *schedules*, using synthetic units whose cost is a sleep; and
``scripts/bench_real_data.py`` measured what happens when every unit contends for
the same Postgres. Each wrote its own artifact, and the only place the two were
ever reconciled was a commit message — which is how an earlier draft came to
claim the synthetic numbers bounded the real one. They do not. They bracket it
from opposite sides, and that is the finding:

    small units  →  dispatch overhead rules them out
    large units  →  database contention caps them

A reader holding one artifact can reach neither conclusion. So this runs both
and writes one document, with both halves and the bracket computed across them.

Usage::

    DATABASE_URL_DIRECT=postgresql://localhost/reimagina_real_b \\
      python scripts/benchmark.py

    python scripts/benchmark.py --synthetic-only     # no real database needed

⚠ The track is taken from ``factory.DEFAULT_MODE``, never hardcoded — the one
place each branch names its engine. This file, ``bench_real_data.py`` and
``perf/workloads.py`` are therefore byte-identical on both worktrees, which is
what makes the two reports comparable at all.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "py"))

from reimagina.orchestration.factory import DEFAULT_MODE  # noqa: E402

TRACK = DEFAULT_MODE


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run_synthetic(reps: int, scale: str) -> dict | None:
    """Drive the pytest suite, then read the artifact it writes.

    Invoked as a subprocess rather than imported. The suite is a pytest session
    with session-scoped fixtures that start workers and sweep database rows;
    calling into it in-process would run those fixtures inside whatever state
    this script already has, and the engine-backed tracks register a worker on
    import. A subprocess is the boundary the suite already assumes.
    """
    out = ROOT / "artifacts" / f"workloads-{TRACK}.json"
    env = {**os.environ, "REIMAGINA_BENCH": "1", "REIMAGINA_BENCH_SCALE": scale}
    if reps:
        env["REIMAGINA_BENCH_REPS"] = str(reps)
    print(f"── synthetic suite ({scale} scale) " + "─" * 40)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/workloads", "-q"],
        cwd=ROOT,
        env=env,
    )
    if r.returncode != 0:
        print(f"⚠ synthetic suite exited {r.returncode}; reading artifact anyway")
    return _load(out)


def run_real(scope: int, reps: int) -> dict | None:
    """Run the real-catalog benchmark, unless there is no database to run it on.

    Absence is reported, never silently skipped: a report with the real half
    missing must say so, because the whole point of pairing them is that either
    number alone supports the wrong conclusion.
    """
    if not os.environ.get("DATABASE_URL_DIRECT"):
        print("── real data: SKIPPED (DATABASE_URL_DIRECT unset) " + "─" * 14)
        return None
    out = ROOT / "artifacts" / f"workloads-real-{TRACK}.json"
    print("── real data " + "─" * 52)
    r = subprocess.run(
        [
            sys.executable,
            "scripts/bench_real_data.py",
            "--scope",
            str(scope),
            "--reps",
            str(reps),
            "--out",
            str(out),
        ],
        cwd=ROOT,
    )
    if r.returncode != 0:
        print(f"⚠ real-data bench exited {r.returncode}")
    return _load(out)


def _speedups(synthetic: dict | None) -> dict[str, float]:
    """Per-workload engine speedup vs `sync`, from whatever shape the report has.

    Tolerant of a missing entry rather than assuming every workload ran: a
    partial suite should still produce a partial bracket, clearly labelled,
    instead of a KeyError that loses the half that did run.
    """
    out: dict[str, float] = {}
    for wid, entry in (synthetic or {}).get("workloads", {}).items():
        modes = entry.get("modes", entry)
        sync = (modes.get("sync") or {}).get("makespan_ms")
        eng = (modes.get(TRACK) or {}).get("makespan_ms")
        if sync and eng:
            out[wid] = round(sync / eng, 3)
    return out


def bracket(synthetic: dict | None, real: dict | None) -> dict:
    """The one number neither half produces alone."""
    sp = _speedups(synthetic)
    real_sp = None
    modes = (real or {}).get("modes", {})
    sync = (modes.get("sync") or {}).get("makespan_ms")
    eng = (modes.get(TRACK) or {}).get("makespan_ms")
    if sync and eng:
        real_sp = round(sync / eng, 3)
    return {
        "synthetic_speedups": sp,
        "synthetic_median": round(median(sp.values()), 3) if sp else None,
        "real_speedup": real_sp,
        "reading": _reading(round(median(sp.values()), 3) if sp else None, real_sp),
    }


def _reading(syn: float | None, real: float | None) -> str:
    if syn is None and real is None:
        return "no measurement"
    if syn is None:
        return f"real data only: {real}x — synthetic half missing, no bracket"
    if real is None:
        return f"synthetic only: median {syn}x — real half missing, no bracket"
    lo, hi = sorted((syn, real))
    return (
        f"bracket {lo}x-{hi}x — synthetic median {syn}x (dispatch overhead), "
        f"real data {real}x (database contention). A service ops-plane wins on "
        f"wall-clock only between them."
    )


# ⚠ Smoke scale multiplies every unit cost by 0.1 against a 5 ms floor, so units
# run 5-150 ms against ~684 ms of measured dispatch lead. The engine then reads
# ~0.02x — two orders below its full-scale figure, and for a reason that is
# entirely the scale rather than the engine. That number is one copy-paste from
# a slide, so the artifact carries its own disclaimer rather than relying on the
# reader to notice `"scale": "smoke"` three keys away.
NOT_QUOTABLE = (
    "⚠ SMOKE SCALE — NOT A MEASUREMENT. Units are shrunk 10x against a fixed "
    "per-unit dispatch cost, so the engine is penalised by the scale itself. "
    "Use --scale full for any number that leaves this machine."
)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scope", type=int, default=0, help="blocks; 0 = every block")
    p.add_argument("--reps", type=int, default=3, help="first is warm-up")
    p.add_argument("--scale", default="full", choices=("smoke", "full"))
    p.add_argument("--synthetic-only", action="store_true")
    p.add_argument("--out", default=f"artifacts/benchmark-{TRACK}.json")
    a = p.parse_args(argv)

    synthetic = run_synthetic(a.reps, a.scale)
    real = None if a.synthetic_only else run_real(a.scope, a.reps)

    doc = {
        "track": TRACK,
        "scale": a.scale,
        "reps": a.reps,
        "synthetic": synthetic,
        "real": real,
        "bracket": bracket(synthetic, real),
    }
    if a.scale != "full":
        doc["quotable"] = False
        doc["warning"] = NOT_QUOTABLE
        doc["bracket"]["reading"] = f"{NOT_QUOTABLE} ({doc['bracket']['reading']})"
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True))

    print("\n" + "═" * 64)
    print(f"track: {TRACK}")
    print(doc["bracket"]["reading"])
    print(f"→ {out}")
    print(
        "Compare the two tracks:  python -m reimagina.perf.workload_report "
        "artifacts/benchmark-dbos.json artifacts/benchmark-hatchet.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
