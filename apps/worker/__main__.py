"""Entry-point for `python -m apps.worker` — the ops-plane worker."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `reimagina.*` is importable from a clean checkout without an editable
# install, mirroring apps/api/__main__.
_PACKAGES_DIR = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

import argparse  # noqa: E402

from .app import run_dbos_worker, run_worker  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.worker",
        description="Drain ops-plane work.",
    )
    parser.add_argument(
        "--engine",
        choices=("dbos", "procrastinate"),
        default="dbos",
        help=(
            "Which ops-plane to drain (default: dbos). The Procrastinate "
            "runtime is kept while the two are compared side by side."
        ),
    )
    parser.add_argument(
        "--queues",
        help=(
            "Comma-separated queue names to drain. Default: compute,control. "
            "Run one process per resource profile rather than raising "
            "--concurrency for blocking compute."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Jobs run at once in this process (default: 1). Procrastinate runs "
            "sync tasks in a thread, so raising this does not parallelise "
            "CPU-bound work and risks starving the worker heartbeat."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.engine == "dbos":
        run_dbos_worker()
        return
    queues = (
        [q.strip() for q in args.queues.split(",") if q.strip()]
        if args.queues
        else None
    )
    run_worker(queues, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
