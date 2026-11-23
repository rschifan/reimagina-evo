"""Entry-point for `python -m apps.worker` — the DBOS ops-plane worker."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `reimagina.*` is importable from a clean checkout without an editable
# install, mirroring apps/api/__main__.
_PACKAGES_DIR = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

import argparse  # noqa: E402

from .app import run_dbos_worker  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m apps.worker",
        description=(
            "Run a DBOS worker: register the workflows, drain the unit queue, "
            "and recover this executor's own interrupted runs."
        ),
        epilog=(
            "There are no queue or concurrency flags. Concurrency belongs to "
            "the registered queue (REIMAGINA_UNIT_CONCURRENCY), not to the "
            "process, and there is only one queue to drain. Recovery is scoped "
            "by REIMAGINA_EXECUTOR_ID, which defaults to '<hostname>:worker': "
            "give each worker on the same host a distinct one."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    _parse_args(argv)
    run_dbos_worker()


if __name__ == "__main__":
    main()
