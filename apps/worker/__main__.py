"""Entry-point for `python -m apps.worker` — the Hatchet ops-plane worker."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `reimagina.*` is importable from a clean checkout without an editable
# install, mirroring apps/api/__main__.
_PACKAGES_DIR = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

import argparse  # noqa: E402

from .app import run_hatchet_worker  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.worker",
        description=(
            "Run a Hatchet worker: register the indicator DAG with the engine "
            "and execute the units it assigns."
        ),
        epilog=(
            "There are no queue flags. Readiness, ordering and dispatch belong "
            "to the engine, so a worker has nothing to select — it takes what "
            "it is given. A running engine (HATCHET_CLIENT_TOKEN) and "
            "DATABASE_URL_DIRECT are both required."
        ),
    )
    parser.add_argument("--name", help="Worker name shown in the dashboard.")
    parser.add_argument(
        "--slots",
        type=int,
        help=(
            "Units run at once in this process (default: "
            "REIMAGINA_UNIT_CONCURRENCY, or 2). Each in-flight unit holds its "
            "own database connection for minutes, so budget this against "
            "max_connections, not against cores."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    run_hatchet_worker(name=args.name, slots=args.slots)


if __name__ == "__main__":
    main()
