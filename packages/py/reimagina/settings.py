"""Tuning knobs for the ops plane, with their defaults in one place.

Sibling of :mod:`reimagina.paths`, and the same idea: a value that several
modules read should have exactly one definition of what it means when unset.

⚠ **Read lazily, on every call — never cached, never resolved at import.**
Twenty-two places across the tests and scripts set these with
``monkeypatch.setenv`` or ``os.environ[...]`` *after* the module is imported,
and the workload harness sets ``REIMAGINA_UNIT_CONCURRENCY`` from its own
``--slots`` before launching an executor. A settings object frozen at import —
which is what a `pydantic-settings` model would give — would silently ignore all
of them. The laziness is the feature; do not "optimise" it into a singleton.

Only knobs read from more than one module live here. A variable with a single
reader is clearer at its point of use.
"""

from __future__ import annotations

import os

#: Units dispatched per worker process.
#:
#: Measured on the real catalog (13 indicators, 1500 blocks, 3 reps), with
#: as-completed dispatch:
#:
#:     slots   makespan   parallelism   efficiency
#:     1         6605ms          0.81         1.00
#:     2         3614ms          1.55         0.91
#:     4         2181ms          2.73         0.76
#:     8         1691ms          3.92         0.49   <- default
#:     16        1629ms          4.03         0.25
#:
#: The ceiling is the graph, not the engine: a DAG's average parallelism is
#: work/span, here 4.03, and 16 slots measure exactly that. 8 is the latency
#: choice; lower it toward 4 where many runs execute concurrently and
#: throughput matters more than one run's wall clock.
DEFAULT_UNIT_CONCURRENCY = 8


def unit_concurrency() -> int:
    """Units per worker. ``REIMAGINA_UNIT_CONCURRENCY``.

    ⚠ This default was restated as ``4`` in the benchmark script and the
    workload harness while the ops plane shipped ``8``, so running either
    without an explicit ``--slots`` measured a concurrency the system does not
    use. That is the drift this module exists to prevent.
    """
    return int(
        os.environ.get("REIMAGINA_UNIT_CONCURRENCY", str(DEFAULT_UNIT_CONCURRENCY))
    )
