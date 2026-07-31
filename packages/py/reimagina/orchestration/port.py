"""Orchestrator port (Phase 3, component 13).

The narrow interface the application depends on to run a scenario evaluation and
read its status, so the engine behind it can be swapped without touching
callers. That swap is the point: this repository is about to fork into two
branches carrying different ops-planes, and everything either branch keeps has
to be expressible here.

Deliberately leaks no engine types — only plain dataclasses. A signature that
mentioned a queue, a workflow handle or a run table would be a signature one of
the two branches could not implement.

Construct implementations through ``orchestration.factory.get_orchestrator``
rather than by name: the factory is the one place an ops-plane swap edits, and
it is the seam tests monkeypatch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
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


@dataclass(frozen=True)
class RunEvent:
    """One progress update, as data — never a pre-formatted SSE string.

    Framing it for the wire is the API route's job (``frames.event_frame``), so
    an orchestrator never has to know it is feeding an HTTP endpoint.

    ``indicator=None`` marks the run-level event carrying the overall status;
    that is the event that ends a subscription.
    """

    run_id: str
    indicator: str | None
    status: str
    detail: str | None = None


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
    ) -> RunHandle:
        """Start a run over the dirty set implied by the edit; return its handle.

        ⚠ **Who commits differs by implementation, and the caller must know
        which it has.** This is stated here because it is the one part of the
        contract the type signature cannot carry, and every caller so far has
        rediscovered it the hard way.

        ``sync`` executes every unit *inside ``conn``'s transaction* and commits
        nothing. The caller owns that transaction and decides: the CLI wraps the
        call in ``with conn:`` so a clean return commits, and a harness that
        simply closes the connection silently discards the entire run — results,
        ``job_run`` provenance and all.

        An engine-backed implementation commits ``conn`` at most for its own
        submit-side bookkeeping, and the *units* run later on connections the
        caller never sees, each committing itself. Nothing the caller does to
        ``conn`` afterwards can roll a unit back.

        So a caller that measures or compares the two must commit explicitly, or
        it is comparing durability semantics rather than the thing it thinks it
        is measuring — one side paying per-unit commits and the other paying
        none. ``scripts/bench_real_data.py`` did exactly that.
        """
        ...

    def get_run(self, run_id: str) -> RunStatus | None:
        """Read a run's status, or None if the engine has never heard of it.

        ⚠ Takes **no connection**, and that is a deletion rather than an
        omission. It used to take one "so the port stays implementable by a
        track that keeps run tables" — and no implementation ever read it: the
        Hatchet one ignored it outright, and the synchronous one passed it down
        to an in-memory store that ignored it too. The cost was not cosmetic:
        `GET /jobs/{id}` and the SSE route each held a pooled database
        connection for the whole of a request that never touched the database,
        and the SSE one held it for the lifetime of the stream.

        A parameter every implementation ignores is not a seam, it is a
        liability with a docstring. `cancel_run` keeps its connection because
        `pg_cancel_backend` genuinely needs one.
        """
        ...

    def cancel_run(self, conn: object, run_id: str) -> bool:
        """Stop a run; True if it was live, False if unknown or already terminal.

        Marks every non-terminal unit ``cancelled`` and closes the run. It does
        **not** promise that in-flight compute stops: no engine can interrupt a
        thread blocked in a psycopg C call, so a unit already executing runs to
        its next boundary. Killing the backend is a separate mechanism.
        """
        ...

    def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield progress events until the run-level terminal event, then stop.

        The first event is a snapshot of the run's current status, so a late
        subscriber is not left waiting on a run that already finished.

        Takes no connection: every engine has its own progress transport
        (LISTEN/NOTIFY here, workflow streams elsewhere) and none of them is the
        caller's pooled connection. Async because the service engines expose
        async iterators; an engine whose stream is a sync generator wraps it.
        """
        ...
