"""Entry-point for `python -m apps.worker` — the Procrastinate worker."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `reimagina.*` is importable from a clean checkout without an editable
# install, mirroring apps/api/__main__.
_PACKAGES_DIR = Path(__file__).resolve().parents[2] / "packages" / "py"
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

from .app import run_worker  # noqa: E402


def main() -> None:
    run_worker()


if __name__ == "__main__":
    main()
