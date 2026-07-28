"""In-memory run store — the ops-plane-free implementation of the store shape.

Mirrors the four ``reimagina.orchestration.runstore`` functions the synchronous
orchestrator actually calls — ``create_run``, ``record_units``, ``finalize_run``,
``get_run`` — so either can be injected without an adapter. It exists so that
orchestrator, a reference implementation of the port used by tests and by no
production path, does not drag ``jobs_runtime`` along with it: an ops-plane that
drops those tables can delete ``runstore`` without touching the synchronous path.

Deliberately not a full mirror. ``set_unit_status`` and ``list_units`` were here
and unused, and the copy of ``set_unit_status`` had drifted — it lacked the
terminal-state guard ``runstore``'s gained, so the two stores disagreed on the
one behaviour a mirror is supposed to guarantee. An unused method that can be
wrong is worse than an absent one.

``conn`` is accepted and ignored throughout, purely to keep the signatures
interchangeable with the Postgres store.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class InMemoryRunStore:
    """Per-instance run state. Not thread-safe; not durable. That is the point."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def create_run(
        self,
        conn: Any,
        *,
        run_id: str,
        scenario_id: str | None,
        code_version: str,
        aoi_id: str | None = None,
        status: str = "running",
    ) -> bool:
        self._runs[run_id] = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "aoi_id": aoi_id,
            "code_version": code_version,
            "status": status,
            "units": [],
        }
        return True

    def record_units(
        self, conn: Any, *, run_id: str, units: Sequence[dict[str, Any]]
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        run["units"] = [
            {
                "indicator": u["indicator"],
                "features": list(u.get("features", [])),
                "status": u.get("status", "queued"),
                "detail": u.get("detail"),
            }
            for u in units
        ]

    def finalize_run(self, conn: Any, *, run_id: str, status: str) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            run["status"] = status

    def get_run(self, conn: Any, run_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "units": [dict(u) for u in run["units"]],
        }
