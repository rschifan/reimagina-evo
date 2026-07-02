"""Provenance helpers (F8) — the code version stamped on every run/result.

``code_version()`` returns the current git commit SHA (or ``"dev"`` outside a
checkout), so ``job_run.code_version`` / ``indicator_result.model_version`` trace
each output to the exact code that produced it, instead of the literal ``"dev"``.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def code_version() -> str:
    """Current git commit SHA, cached; ``"dev"`` when git/checkout unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return "dev"
    return result.stdout.strip() or "dev"
