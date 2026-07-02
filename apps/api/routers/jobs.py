"""Intake API (Phase 3, component 2) — submit a scenario evaluation, read status.

``POST /jobs`` enqueues a run on the Procrastinate queue and returns ``202``
immediately (no compute in the request path — a worker drains it). The enqueue
runs on a dedicated committed connection, NOT the API's rollback-scoped pool.
``GET /jobs/{run_id}`` reads the persisted run + per-unit status.

Requires ``DATABASE_URL_DIRECT`` + a running ``python -m apps.worker``.
"""

from __future__ import annotations

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import api_dsn, get_db_conn

router = APIRouter(prefix="/jobs", tags=["jobs"])


class SubmitRunBody(BaseModel):
    changed_attributes: list[str]
    edited_features: list[str]
    scenario_id: str | None = None
    aoi_id: str | None = None


class SubmitRunResponse(BaseModel):
    run_id: str
    status: str


class UnitView(BaseModel):
    indicator: str
    status: str
    detail: str | None = None


class RunView(BaseModel):
    run_id: str
    status: str
    units: list[UnitView] = []


@router.post("", status_code=202, response_model=SubmitRunResponse)
def submit_run(body: SubmitRunBody) -> SubmitRunResponse:
    from reimagina.db import connect
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )
    from reimagina.provenance import code_version

    # Enqueue on a dedicated committed connection — the API pool connection
    # rolls back on teardown, which would discard the run + queued units.
    with connect(api_dsn()) as conn:
        handle = ProcrastinateOrchestrator().submit_run(
            conn,
            changed_attributes=body.changed_attributes,
            edited_features=body.edited_features,
            scenario_id=body.scenario_id,
            aoi_id=body.aoi_id,
            code_version=code_version(),
        )
    return SubmitRunResponse(run_id=handle.run_id, status="queued")


@router.get("/{run_id}", response_model=RunView)
def get_run(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunView:
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )

    status = ProcrastinateOrchestrator().get_run(conn, run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return RunView(
        run_id=status.run_id,
        status=status.status,
        units=[
            UnitView(indicator=u.indicator, status=u.status, detail=u.detail)
            for u in status.units
        ],
    )
