"""Orchestrator port (Phase 3, component 13).

The narrow interface the application depends on to run a scenario evaluation and
read its status — so the whole engine behind it can be swapped (the synchronous
in-process executor now, a Procrastinate distributor next, a Dagster adapter
later) without touching callers. Deliberately leaks no Hamilton/Procrastinate
types — only plain dataclasses.

``subscribe_progress`` (live SSE stream) lands with the async distributor in a
later phase; the port stays minimal until it has a real implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class UnitStatusView:
    indicator: str
    status: str  # succeeded | failed | skipped | running | queued
    detail: str | None = None


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str  # succeeded | partial | failed | running | queued
    units: tuple[UnitStatusView, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    # The synchronous orchestrator finishes the run before returning, so it
    # populates ``status`` here; an async orchestrator leaves it None and the
    # caller polls ``get_run``.
    status: RunStatus | None = None


class Orchestrator(Protocol):
    def submit_run(
        self,
        conn: object,
        *,
        changed_attributes: Iterable[str],
        edited_features: Iterable[str],
        scenario_id: str | None = None,
        aoi_id: str | None = None,
        code_version: str = "dev",
    ) -> RunHandle: ...

    def get_run(self, conn: object, run_id: str) -> RunStatus | None: ...
