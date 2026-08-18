"""The one place an :class:`~reimagina.orchestration.port.Orchestrator` is named.

Every caller asks for a mode rather than naming a class, so swapping the
ops-plane is a rewrite of *this* file instead of a hunt through the API router,
the CLI and four test modules — and tests monkeypatch ``get_orchestrator``
rather than a class path only one implementation has.

This branch runs the **DBOS** ops-plane: durable execution as a library, with
checkpoints in our own Postgres and no standing service.

``dbos``
    Submit a durable workflow and return. The production default. Because DBOS
    is a library rather than a service, the process that submits can also drain,
    so a worker fleet is how this scales rather than how it works at all.
``sync``
    Compute in-process with no engine. No production caller; it exists so the
    conformance suite has a target needing neither engine nor database, and it
    is the one path that still runs when the ops-plane is broken.

Two modes, not four. ``detach`` and ``embedded`` used to be *engines* — enqueue
and return, versus enqueue and drain in a bounded in-process worker. Under a
durable-execution library that is not an engine choice at all, because one
process can do both, so asking the factory for either now raises.

⚠ The CLI still spells its ``--detach`` flag and its default the same way, and
that is not an oversight: there they name whether the *caller* waits, which is
a real choice and belongs to the caller. Same words, different layer. See
``apps/cli/indicators.py``.

Imports are deferred on purpose: the engine pulls its client library in at
module scope, and ``sync`` must stay importable without it.
"""

from __future__ import annotations

import os

from reimagina.orchestration.port import Orchestrator

DEFAULT_MODE = "dbos"
MODES = ("dbos", "sync")

DURABLE_MODES = ("dbos",)
"""Modes that persist a run somewhere a *different* process can read it.

``sync`` does not: it keeps run state in memory, per orchestrator instance. That
makes it fine as an explicit choice and unusable as a deployment default —
``POST /jobs`` names its own mode to honour its 200-vs-202 contract, while
``GET /jobs/{run_id}`` and the SSE route take the configured one, so a global
``sync`` would have writes going to Postgres and reads coming from an empty
dict. Hence the environment may only select from this list.
"""


def get_orchestrator(mode: str | None = None) -> Orchestrator:
    """Build the orchestrator for ``mode``, or for ``REIMAGINA_ORCHESTRATOR``."""
    if mode is None:
        mode = os.environ.get("REIMAGINA_ORCHESTRATOR") or DEFAULT_MODE
        if mode not in DURABLE_MODES:
            raise ValueError(
                f"REIMAGINA_ORCHESTRATOR={mode!r} is not a deployable mode. "
                f"Choose one of: {', '.join(DURABLE_MODES)}. "
                "A mode that keeps run state in memory cannot be a deployment "
                "default: this factory builds a fresh instance per call, so the "
                "request that reads a run is never the one that wrote it. Pass "
                "such a mode explicitly, and hold on to the instance."
            )

    if mode == "dbos":
        from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator

        return DBOSOrchestrator()

    if mode == "sync":
        from reimagina.orchestration.synchronous import SynchronousOrchestrator

        return SynchronousOrchestrator()

    raise ValueError(
        f"unknown orchestrator mode: {mode!r}. Known modes: {', '.join(MODES)}."
    )
