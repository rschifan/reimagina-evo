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

# ── Status vocabulary ────────────────────────────────────────────────────────
# One definition, imported by every executor. It lives here rather than in the
# coordinator because the port is the module no ops-plane implementation
# deletes; the scheduler and the in-process executor both come and go.
#
# NOTE: the database is a separate, *not* currently reconciled layer —
# job_run, orchestration_run and job_unit each carry a different status CHECK
# (`skipped` is legal only on job_unit, `partial` only on orchestration_run).
# Unifying Python does not unify those; widening them is a migration decision.

UNIT_TERMINAL_STATES = frozenset({"succeeded", "failed", "skipped", "cancelled"})
"""Unit statuses from which no further transition happens."""

UNIT_UNSATISFIED_STATES = frozenset({"failed", "skipped", "cancelled"})
"""Unit statuses that make a dependent unrunnable."""

RUN_TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
"""Run statuses that close a progress stream."""


def overall_status(statuses: Iterable[str]) -> str:
    """Roll unit statuses into a run status.

    succeeded (all ok) / partial (some ok) / cancelled (none ok, none failed, at
    least one cancelled) / failed (anything else, including an all-skipped run —
    nothing ran, so the run did not achieve its purpose).

    ``cancelled`` only wins when no unit actually failed: a run that both failed
    and was cancelled is a failure, and reporting it as cancelled would hide the
    error. ``orchestration_run.status`` has no ``skipped``, which is why the
    all-skipped case folds into ``failed`` rather than gaining a fourth value.
    """
    seen = set(statuses)
    if not seen or seen <= {"succeeded"}:
        return "succeeded"
    if "succeeded" in seen:
        return "partial"
    if "failed" not in seen and "cancelled" in seen:
        return "cancelled"
    return "failed"


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
