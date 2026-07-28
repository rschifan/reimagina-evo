"""In-memory run store — the ops-plane-free implementation of the store shape.

Mirrors the subset of ``reimagina.orchestration.runstore``'s signatures that a
port implementation needs, so either can be injected without an adapter. It
exists so the synchronous orchestrator — a reference implementation of the port,
used by tests and by no production path — does not drag `jobs_runtime` along
with it. An ops-plane that drops those tables can then delete `runstore` without
touching the synchronous path.

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

    def set_unit_status(
        self,
        conn: Any,
        *,
        run_id: str,
        indicator_slug: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        run = self._runs.get(run_id)
        if run is None:
            return
        for unit in run["units"]:
            if unit["indicator"] == indicator_slug:
                unit["status"] = status
                unit["detail"] = detail
                return

    def list_units(self, conn: Any, run_id: str) -> list[dict[str, Any]]:
        run = self._runs.get(run_id)
        return list(run["units"]) if run else []

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
