"""The one place an :class:`~reimagina.orchestration.port.Orchestrator` is named.

Every caller asks for a mode; nothing outside this module imports a concrete
orchestrator class. Swapping the ops-plane is then a rewrite of *this* file
rather than a hunt through the API router, the CLI and four test modules — and
tests monkeypatch ``get_orchestrator`` instead of a class path that only one
implementation has.

Modes are the vocabulary the CLI already exposes as ``--mode``:

``detach``
    Enqueue and return immediately; a separate worker fleet drains it. The
    production default.
``embedded``
    Enqueue and drain a bounded in-process worker, returning the finished run.
    No standing worker needed — the interactive default.
``sync``
    Compute in-process with no queue at all. No production caller today; it
    exists so the conformance suite has a target that needs no engine and no
    database.

Imports are deferred into each branch on purpose: ``detach`` and ``embedded``
pull the queue library in at module scope, and ``sync`` must stay importable
without it.
"""

from __future__ import annotations

import os

from reimagina.orchestration.port import Orchestrator

DEFAULT_MODE = "detach"
MODES = ("detach", "embedded", "sync")


def get_orchestrator(mode: str | None = None) -> Orchestrator:
    """Build the orchestrator for ``mode``, or for ``REIMAGINA_ORCHESTRATOR``."""
    mode = mode or os.environ.get("REIMAGINA_ORCHESTRATOR") or DEFAULT_MODE

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
