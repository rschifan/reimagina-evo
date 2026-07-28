"""The one place an :class:`~reimagina.orchestration.port.Orchestrator` is named.

Every caller asks for a mode rather than naming a class, so swapping the
ops-plane is a rewrite of *this* file instead of a hunt through the API router,
the CLI and four test modules — and tests monkeypatch ``get_orchestrator``
rather than a class path only one implementation has.

This branch runs the **DBOS** ops-plane: durable execution as a library, with
checkpoints in our own Postgres and no standing service.

``dbos``
    Submit a durable workflow and return; a worker drains it. The production
    default here. Because DBOS is a library rather than a service, the same
    process can both submit and drain, so this needs no separate fleet to be
    useful — it only needs one to scale.
``sync``
    Compute in-process with no engine at all. No production caller; it exists
    so the conformance suite has a target needing neither engine nor database,
    and it is the one path that still runs when the ops-plane is broken.

The Procrastinate modes (``detach``, ``embedded``) are still present while the
two ops-planes are compared side by side; they go when this branch drops that
engine.

Imports are deferred into each branch on purpose: every engine pulls its client
library in at module scope, and ``sync`` must stay importable without any of
them.
"""

from __future__ import annotations

import os

from reimagina.orchestration.port import Orchestrator

DEFAULT_MODE = "dbos"
MODES = ("dbos", "detach", "embedded", "sync")

DURABLE_MODES = ("dbos", "detach", "embedded")
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

    if mode == "detach":
        from reimagina.orchestration.procrastinate_orchestrator import (
            ProcrastinateOrchestrator,
        )

        return ProcrastinateOrchestrator()

    if mode == "embedded":
        from reimagina.orchestration.embedded import EmbeddedOrchestrator

        return EmbeddedOrchestrator()

    if mode == "sync":
        from reimagina.orchestration.synchronous import SynchronousOrchestrator

        return SynchronousOrchestrator()

    raise ValueError(
        f"unknown orchestrator mode: {mode!r}. Known modes: {', '.join(MODES)}."
    )
