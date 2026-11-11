"""Intake API (Phase 3, component 2) — submit a scenario evaluation, read status.

``POST /jobs`` enqueues a run and returns ``202`` immediately (no compute in the
request path — a worker drains it). Passing ``wait: true`` instead drains the
run in-request and returns ``200`` with the
finished status (a convenience for simple/test clients; production stays
fire-and-forget). The enqueue runs on a dedicated committed connection, NOT the
API's rollback-scoped pool. ``GET /jobs/{run_id}`` reads the persisted run +
per-unit status.

Every path goes through ``orchestration.factory.get_orchestrator`` — this module
names no engine, so swapping the ops-plane does not touch it. ``wait`` selects
whether to block, not which engine to use: under a durable-execution library one
process can both submit and drain, so the 202-vs-200 split is a response-shape
decision rather than a routing one.

Requires ``DATABASE_URL_DIRECT`` + a running ``python -m apps.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from enum import Enum

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reimagina.storage._utils import is_run_id

from ..deps import api_dsn, get_backend, get_db_conn

# Run statuses from which nothing further happens; mirrors port.RUN_TERMINAL_STATES
# without importing the ops-plane into a module that names no engine.
_RUN_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})

_STREAM_MAX_SECONDS = 300.0
_HEARTBEAT_SECONDS = 15.0

router = APIRouter(prefix="/jobs", tags=["jobs"])


class RunStatus(str, Enum):
    """The ``orchestration_run.status`` CHECK vocabulary (r028), as a filter type.

    A ``str`` mixin, not a bare ``Enum``: FastAPI binds repeated query params to
    Enum *members*, and psycopg2 can only adapt those if they are also strings —
    a plain Enum reaches the driver as ``can't adapt type``, which the API's
    error handler turns into a 500 with an aborted transaction, on the happy path.
    """

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelling = "cancelling"
    cancelled = "cancelled"


class RunRow(BaseModel):
    run_id: str
    status: str
    scenario_id: str | None = None
    aoi_id: str | None = None
    code_version: str
    submitted_at: datetime
    completed_at: datetime | None = None
    units_total: int = 0
    units_terminal: int = 0
    units_succeeded: int = 0
    units_failed: int = 0
    units_running: int = 0
    # Stall detection reads this: a run whose units all finished but which never
    # closed is one whose workflow died past its last step. See the Ops plan, H5.
    units_last_terminal_at: datetime | None = None


class ExecutorRow(BaseModel):
    executor_id: str
    role: str
    app_version: str | None = None
    # Seconds, not the view's ``interval`` — a timedelta here is a 500.
    heartbeat_age_s: float


class RunListResponse(BaseModel):
    runs: list[RunRow] = []
    # The dashboard computes every elapsed time against this rather than the
    # browser clock, so a skewed client neither hides nor invents a stalled run.
    server_now: datetime
    total: int = 0
    # Unfiltered on purpose: a count that shrank to match the active filter would
    # make the chips useless for finding what you just filtered away.
    status_counts: dict[str, int] = {}
    executors: list[ExecutorRow] = []


class UnitDetail(BaseModel):
    indicator: str
    status: str
    detail: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Prerequisites *within this run*. The registry knows the full dependency
    # graph, but a unit whose input was not part of this run was not waiting on
    # it — that input was already materialised — so showing the whole graph
    # would misrepresent why a unit started when it did.
    depends_on: list[str] = []


class RunDetailResponse(BaseModel):
    run: RunRow
    units: list[UnitDetail] = []
    server_now: datetime


class SubmitRunBody(BaseModel):
    # Optional: when a scenario_id is given and these are omitted, the change-set
    # is derived from the scenario's overrides (the override rows are the diff).
    changed_attributes: list[str] | None = None
    edited_features: list[str] | None = None
    scenario_id: str | None = None
    aoi_id: str | None = None
    # Recompute exactly these indicators, skipping change-set derivation.
    # Used by the Ops launch form and by retry-the-failed-units.
    indicators: list[str] | None = None
    # Whole datasets that changed — "a street was added to the road network".
    # Resolved to the indicators declaring an edge to them, plus everything
    # downstream. Not expressible as a change-set: those name attributes.
    changed_datasets: list[str] | None = None
    # Opt-in: wait for the run to finish and return
    # the finished status with 200 instead of a 202 + poll. Convenience for
    # simple/test clients; production should stay fire-and-forget (default) with
    # a real worker fleet, since this ties up the request for the whole compute.
    wait: bool = False


class SubmitRunResponse(BaseModel):
    # None for a "noop" submit: the dirty-set was empty, so no run row exists
    # and GET /jobs/{run_id} would 404. Handing back an unusable id makes a
    # client following the documented poll loop fail after a *successful* submit.
    run_id: str | None
    status: str


class UnitView(BaseModel):
    indicator: str
    status: str
    detail: str | None = None


class RunView(BaseModel):
    run_id: str
    status: str
    units: list[UnitView] = []


def _submit_response(run_id: str, status: str) -> SubmitRunResponse:
    """Suppress the run_id on a noop — nothing was persisted to poll for."""
    return SubmitRunResponse(run_id=None if status == "noop" else run_id, status=status)


@router.post("", status_code=202, response_model=SubmitRunResponse)
def submit_run(body: SubmitRunBody, response: Response) -> SubmitRunResponse:
    from reimagina.db import connect
    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.orchestration.port import OpsPlaneNotProvisioned
    from reimagina.provenance import code_version

    orchestrator = get_orchestrator()

    indicators = body.indicators
    if body.changed_datasets:
        from reimagina.indicators.catalog import SHIPPED_INDICATORS
        from reimagina.orchestration.impact import indicators_for_datasets

        resolved = indicators_for_datasets(body.changed_datasets, SHIPPED_INDICATORS)
        indicators = (
            sorted(set(indicators or ()) | set(resolved))
            if indicators
            else list(resolved)
        )

    # Enqueue on a dedicated committed connection — the API pool connection
    # rolls back on teardown, which would discard the run + queued units.
    #
    # ⚠ `closing()` around it, and the connection itself as the inner context.
    # psycopg2's connection context manager scopes the *transaction*: it commits
    # or rolls back and leaves the socket open. This runs once per submit in a
    # long-lived API process, so the bare `with connect(...)` it replaces leaked
    # a backend on every POST until Postgres ran out. Same reasoning as
    # `dbos_app._pg` and `jobs._lifecycle.record_failed_job_run`; the inner
    # context keeps the commit-on-success the enqueue depends on.
    with contextlib.closing(connect(api_dsn())) as conn, conn:
        try:
            handle = orchestrator.submit_run(
                conn,
                changed_attributes=body.changed_attributes or [],
                edited_features=body.edited_features or [],
                scenario_id=body.scenario_id,
                aoi_id=body.aoi_id,
                code_version=code_version(),
                indicators=indicators,
            )
        except OpsPlaneNotProvisioned as exc:
            # 503, not 500: the request is well-formed and the code is sound —
            # the ops plane's storage has not been created in this database. A
            # 500 would say "a bug, look at the logs"; this says "not ready
            # yet", which is both true and retryable once the remedy named in
            # the message has been applied. The detail is echoed deliberately:
            # it is our own sentence, naming a command, with nothing internal
            # in it.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # "noop" when the run had nothing to recompute; else the queued run_id.
        status = handle.status.status if handle.status else "queued"
        if body.wait and handle.status is None:
            status = _await_terminal(orchestrator, conn, handle.run_id)
            response.status_code = 200  # finished, not merely accepted
        elif body.wait:
            response.status_code = 200

    return _submit_response(handle.run_id, status)


def _await_terminal(orchestrator, conn, run_id: str) -> str:
    """Poll the port until the run is terminal, bounded by the stream deadline.

    Submitting no longer blocks: the ops-plane is a library, so the run is
    durable the moment it is accepted and progresses whether or not anyone is
    waiting. ``wait: true`` is therefore a convenience for simple clients, and
    the wait belongs here rather than in the port — an engine that needed a
    separate worker could not implement a blocking submit at all.
    """
    from reimagina.orchestration.port import RUN_TERMINAL_STATES

    deadline = time.monotonic() + _STREAM_MAX_SECONDS
    while time.monotonic() < deadline:
        conn.rollback()  # end the read txn so the next poll sees fresh rows
        current = orchestrator.get_run(conn, run_id)
        if current is not None and current.status in RUN_TERMINAL_STATES:
            return current.status
        time.sleep(0.25)
    raise HTTPException(
        status_code=504,
        detail=(
            f"run {run_id} did not finish within {_STREAM_MAX_SECONDS:.0f}s; "
            "it is still running — poll GET /jobs/{run_id}"
        ),
    )


def _server_now() -> datetime:
    """One clock for every elapsed time the dashboard renders.

    The page polls every two seconds and shows live durations and a stalled
    badge; computing those against ``Date.now()`` would let a skewed client
    clock invent or suppress both. The server states its own time and the page
    corrects by the offset.
    """
    return datetime.now(timezone.utc)


class ChangeInput(BaseModel):
    """One thing a change-set can name, with the dataset it belongs to."""

    # `mex.block · geometry` rather than a bare `geometry`: the attribute name
    # alone does not say what it is an attribute *of*, and every one of them
    # currently belongs to a different dataset in principle.
    dataset: str
    attribute: str
    indicators: list[str] = []
    # True for the hand-bumped stand-in markers that exist only so the demo can
    # invalidate work. Flagged rather than hidden — they are the only way to
    # express "the POI dataset changed" today, and a caller should know that is
    # what they are reaching for.
    is_version_marker: bool = False


class DatasetDependency(BaseModel):
    """A whole-dataset input — no attribute, so no way to name it in a change-set."""

    dataset: str
    relation: str
    indicators: list[str] = []
    stand_in_attribute: str | None = None


class ChangeVocabulary(BaseModel):
    attributes: list[ChangeInput] = []
    datasets: list[DatasetDependency] = []
    # Registered datasets that no indicator consumes. Changing one recomputes
    # nothing — which is worth showing rather than leaving someone to conclude
    # the planner is broken when a real edit produces an empty dirty set.
    unconsumed: list[str] = []


# The demo-only markers, named where they are interpreted rather than guessed
# at in the UI. `catalog.py` states it: nothing in the ingest path produces
# these; `scripts/seed_demo_attributes.py` writes them and they are bumped by
# hand, standing in for a real version on the dataset edge beside them.
_VERSION_MARKERS = {
    "poi_version": "global.poi",
    "network_version": "global.road_network",
}


@router.get("/attributes", response_model=ChangeVocabulary)
def list_change_inputs() -> ChangeVocabulary:
    """What a change-set can name, derived from the registry's own edges.

    Read off each indicator's ``inputs`` — the typed asset edges — rather than
    the flattened ``reads`` list, because ``reads`` drops the dataset an
    attribute belongs to, which is precisely the thing a caller needs to know.

    Also reports the whole-dataset edges (a POI layer reloaded, a road network
    re-ingested). Those cannot be expressed as a change-set today: the API
    accepts attribute names only, so the honest answer is to surface them with
    the stand-in marker that currently substitutes for them.
    """
    from reimagina.assets.registry import SHIPPED_ASSETS
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    attrs: dict[tuple[str, str], list[str]] = {}
    datasets: dict[tuple[str, str], list[str]] = {}
    # `spec.inputs` is `tuple[AssetEdge, ...]`, so these are plain attribute
    # reads. They were `getattr(edge, "key", None) or getattr(edge, "asset_key",
    # "")` — defensive over a frozen dataclass that has no `key` field at all,
    # so the first half evaluated to None on every edge and only the fallback
    # ever ran. The cost was not just the dead branch: `getattr` with a default
    # erases the type, which is what left `key` as `Any | None` and produced
    # three mypy errors on a value that is always `str`.
    for spec in SHIPPED_INDICATORS:
        for edge in spec.inputs:
            if edge.attributes:
                for name in edge.attributes:
                    attrs.setdefault((edge.asset_key, name), []).append(spec.slug)
            elif not edge.asset_key.startswith("indicator."):
                # Composites depend on other indicators, which the run planner
                # already orders; they are not something a person "changes".
                datasets.setdefault((edge.asset_key, edge.relation), []).append(
                    spec.slug
                )

    consumed = {dataset for dataset, _ in attrs} | {dataset for dataset, _ in datasets}
    unconsumed = sorted(
        spec.asset_key
        for spec in SHIPPED_ASSETS
        if spec.asset_key not in consumed
        and not spec.asset_key.startswith("indicator.")
    )

    return ChangeVocabulary(
        unconsumed=unconsumed,
        attributes=[
            ChangeInput(
                dataset=dataset,
                attribute=name,
                indicators=sorted(set(slugs)),
                is_version_marker=name in _VERSION_MARKERS,
            )
            for (dataset, name), slugs in sorted(attrs.items())
        ],
        datasets=[
            DatasetDependency(
                dataset=dataset,
                relation=rel,
                indicators=sorted(set(slugs)),
                stand_in_attribute=next(
                    (a for a, d in _VERSION_MARKERS.items() if d == dataset), None
                ),
            )
            for (dataset, rel), slugs in sorted(datasets.items())
        ],
    )


class ImpactPreview(BaseModel):
    """What a submit *would* do, without doing it."""

    indicators: list[str] = []
    count: int = 0
    # True when the change-set invalidated nothing. A correct answer, not a
    # failure — and the one a caller most needs distinguished, because the
    # submit that follows would return `noop` and no run id.
    noop: bool = True
    reason: str = ""


@router.post("/preview", response_model=ImpactPreview)
def preview_impact(
    body: SubmitRunBody,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> ImpactPreview:
    """Dry-run the planner: which indicators would this change-set recompute?

    Pure derivation — reads the catalog and, for a scenario or a freshness
    submit, the graph. Persists nothing, enqueues nothing, and takes the pooled
    read connection rather than a committed one precisely because it must not
    be able to write.

    Exists so the recomputation engine can be inspected rather than trusted:
    the Ops launch form shows the answer before you submit, and the catalog page
    uses it to answer "if I changed this, what goes stale?".

    ⚠ Registered before ``/{run_id}``-shaped routes is unnecessary — Starlette
    matches ``/preview`` as its own literal path — but it is a POST while the
    id routes are GETs, so there is no ambiguity either way.
    """
    from reimagina.orchestration.plan import UnknownIndicators, plan_run
    from reimagina.provenance import code_version

    # A dataset-level change is resolved from the registry's edges, then handed
    # to the planner as an explicit indicator list — which is also exactly how
    # it would be submitted, so the preview and the run cannot disagree.
    indicators = body.indicators
    if body.changed_datasets:
        from reimagina.indicators.catalog import SHIPPED_INDICATORS
        from reimagina.orchestration.impact import indicators_for_datasets

        resolved = indicators_for_datasets(body.changed_datasets, SHIPPED_INDICATORS)
        indicators = (
            sorted(set(indicators or ()) | set(resolved))
            if indicators
            else list(resolved)
        )
        if not indicators:
            return ImpactPreview(
                indicators=[],
                count=0,
                noop=True,
                reason="no indicator declares an edge to that dataset",
            )

    try:
        plan = plan_run(
            conn,
            changed_attributes=body.changed_attributes or [],
            edited_features=body.edited_features or [],
            scenario_id=body.scenario_id,
            aoi_id=body.aoi_id,
            code_version=code_version(),
            indicators=indicators,
        )
    except UnknownIndicators as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    slugs = [unit.indicator for unit in plan.units]
    return ImpactPreview(
        indicators=slugs,
        count=len(slugs),
        noop=plan.is_noop,
        reason=_preview_reason(body, slugs),
    )


def _preview_reason(body: SubmitRunBody, slugs: list[str]) -> str:
    """One line naming which planning path answered, so the answer is auditable."""
    if body.changed_datasets:
        return "indicators reading that dataset, plus everything downstream"
    if body.indicators is not None:
        return "named explicitly, in dependency order"
    if slugs and (body.changed_attributes or body.edited_features):
        return "derived from the change-set you named"
    if slugs and body.scenario_id:
        return "derived from that scenario's overrides"
    if slugs:
        return "derived from freshness — these are out of date"
    if body.changed_attributes or body.edited_features:
        return "nothing reads what you changed"
    return "nothing is out of date (freshness counts stale and partial, never absent)"


class RerunResponse(SubmitRunResponse):
    """The *new* run, plus what it was scoped from."""

    rerun_of: str
    indicators: list[str] = []


@router.post("/{run_id}/cancel", response_model=RunView)
def cancel_run(run_id: str) -> RunView:
    """Stop a live run. 404 unknown, 409 already finished.

    ``cancel_run`` returns False for both "unknown" and "already terminal", and
    a 409 for a run that does not exist is the wrong answer — so the existence
    check happens here first.

    ⚠ Its own committed connection, not ``Depends(get_db_conn)``. The port
    implementation calls ``conn.commit()`` itself, so on a pooled connection it
    would commit on a connection this route does not own — worse than the
    rollback it would otherwise get.
    """
    from reimagina.db import connect
    from reimagina.orchestration.factory import get_orchestrator

    if not is_run_id(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    orchestrator = get_orchestrator()
    with contextlib.closing(connect(api_dsn())) as conn, conn:
        current = orchestrator.get_run(conn, run_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        if not orchestrator.cancel_run(conn, run_id):
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} is already {current.status}",
            )
        after = orchestrator.get_run(conn, run_id) or current

    return RunView(
        run_id=after.run_id,
        status=after.status,
        units=[
            UnitView(indicator=u.indicator, status=u.status, detail=u.detail)
            for u in after.units
        ],
    )


@router.post("/{run_id}/rerun", status_code=202, response_model=RerunResponse)
def rerun_run(run_id: str, response: Response) -> RerunResponse:
    """Recompute this run's non-succeeded indicators, as a **new** run.

    Not a resume. A unit cannot be recomputed under its own run id: the child
    workflow id is ``{run_id}-{slug}``, a re-enqueue past recorded history dedups
    on that id rather than minting a second child, and a step that recorded an
    error re-raises it on replay instead of executing. A new run id is the only
    thing that actually recomputes.

    Every non-succeeded unit is included — failed, skipped and cancelled alike.
    ``skipped`` is not optional: a unit is skipped exactly when a dirty input was
    unsatisfied, so rerunning the failure without its skipped dependents would
    leave those stale for good.
    """
    from reimagina.db import connect
    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.provenance import code_version

    if not is_run_id(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    with contextlib.closing(connect(api_dsn())) as conn, conn:
        reader = get_backend(conn).ops_run_reader()
        row = reader.get_run_row(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
        if row["status"] not in _RUN_TERMINAL:
            # Rerunning a live run would race its own remaining units for the
            # same indicators.
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} is still {row['status']} — cancel it first",
            )
        slugs = [
            unit["indicator"]
            for unit in reader.list_units_detailed(run_id)
            if unit["status"] != "succeeded"
        ]
        if not slugs:
            raise HTTPException(
                status_code=409, detail="every indicator in that run succeeded"
            )

        handle = get_orchestrator().submit_run(
            conn,
            changed_attributes=[],
            edited_features=[],
            scenario_id=row["scenario_id"],
            aoi_id=row["aoi_id"],
            code_version=code_version(),
            indicators=slugs,
        )

    status = handle.status.status if handle.status else "queued"
    response.status_code = 202
    return RerunResponse(
        run_id=handle.run_id,
        status=status,
        rerun_of=run_id,
        indicators=slugs,
    )


class DeletedRuns(BaseModel):
    deleted: int


@router.delete("/{run_id}", response_model=DeletedRuns)
def delete_run(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> DeletedRuns:
    """Forget one finished run. 404 unknown, 409 still live.

    Removes the run envelope and its unit rows — the dashboard's history. It
    does **not** touch the values the run computed (``indicator_result``) or its
    provenance (``job_run``): forgetting that a run happened is not undoing what
    it produced.

    A live run is refused rather than deleted. Its workflow still owns these
    rows, so deleting the envelope mid-flight would have the remaining steps
    write into a run that no longer exists — cancel it first.
    """
    from reimagina.db import connect

    if not is_run_id(run_id):
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    reader = get_backend(conn).ops_run_reader()
    row = reader.get_run_row(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    if row["status"] not in _RUN_TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} is still {row['status']} — cancel it first",
        )

    # Its own committed connection: the pooled one rolls back on teardown, which
    # would make this look like it worked and change nothing.
    with contextlib.closing(connect(api_dsn())) as write_conn, write_conn:
        deleted = get_backend(write_conn).ops_run_reader().delete_run(run_id)
    return DeletedRuns(deleted=1 if deleted else 0)


@router.delete("", response_model=DeletedRuns)
def delete_finished_runs() -> DeletedRuns:
    """Forget every finished run, leaving live ones alone.

    Same boundary as the single delete: history goes, computed values stay.
    """
    from reimagina.db import connect

    with contextlib.closing(connect(api_dsn())) as conn, conn:
        deleted = get_backend(conn).ops_run_reader().delete_terminal_runs()
    return DeletedRuns(deleted=deleted)


@router.get("", response_model=RunListResponse)
def list_runs(
    status: list[RunStatus] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunListResponse:
    """The Ops dashboard's index, in one response.

    List, totals, chip counts and executor health together because the page
    polls this every two seconds: splitting executor health into its own route
    would double that against a sixteen-connection pool shared with tiles and
    indicators.

    ⚠ ``status`` needs the explicit ``Query(None)``. With a bare
    ``list[RunStatus] | None`` FastAPI reads a non-scalar annotation as a *body*
    parameter, so ``?status=failed&status=running`` binds to None, returns 200,
    and silently ignores the filter.
    """
    reader = get_backend(conn).ops_run_reader()
    # ``[]`` binds as an empty array, which matches no row — "no filter" has to
    # reach the reader as None, not as the empty list a query string produces.
    statuses = [s.value for s in status] if status else None
    return RunListResponse(
        runs=[
            RunRow(**row)
            for row in reader.list_runs(statuses=statuses, limit=limit, offset=offset)
        ],
        server_now=_server_now(),
        total=reader.count_runs(statuses=statuses),
        status_counts=reader.count_runs_by_status(),
        executors=[
            ExecutorRow(
                executor_id=row["executor_id"],
                role=row["role"],
                app_version=row["app_version"],
                heartbeat_age_s=row["heartbeat_age_s"],
            )
            for row in reader.list_executor_health()
        ],
    )


@router.get("/{run_id}/detail", response_model=RunDetailResponse)
def get_run_detail(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunDetailResponse:
    """One run with per-unit timings — the drill-down the run list links to.

    A separate route rather than widening ``GET /jobs/{run_id}``: that response
    is built from the port's ``RunStatus``/``UnitStatusView``, which carry no
    timestamps, so widening it would be a port change obliging every ops-plane
    implementation for two columns.

    ⚠ Falls back to the port when the envelope row is missing. ``orchestration_run``
    is written by the workflow's *first step*, not by ``submit_run``, so for a
    second or two after launch a purely SQL answer is 404 — which is exactly when
    the user has just been navigated here.
    """
    reader = get_backend(conn).ops_run_reader()
    row = reader.get_run_row(run_id)
    if row is None:
        return _detail_before_envelope(conn, run_id)
    units = reader.list_units_detailed(run_id)
    within = {u["indicator"] for u in units}
    return RunDetailResponse(
        run=RunRow(**row),
        units=[
            UnitDetail(**unit, depends_on=_prerequisites(unit["indicator"], within))
            for unit in units
        ],
        server_now=_server_now(),
    )


def _prerequisites(slug: str, within: set[str]) -> list[str]:
    """This indicator's declared inputs, restricted to the ones in this run."""
    from reimagina.indicators.catalog import SHIPPED_INDICATORS

    for spec in SHIPPED_INDICATORS:
        if spec.slug == slug:
            return sorted({dep for dep, _weight in spec.depends_on} & within)
    return []


def _detail_before_envelope(conn, run_id: str) -> RunDetailResponse:
    """Synthesise a detail response for a run the engine knows and our tables do not."""
    from reimagina.orchestration.factory import get_orchestrator

    status = get_orchestrator().get_run(conn, run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    now = _server_now()
    return RunDetailResponse(
        run=RunRow(
            run_id=status.run_id,
            status=status.status,
            code_version="",
            submitted_at=now,
            units_total=len(status.units),
        ),
        units=[
            UnitDetail(
                indicator=u.indicator, status=u.status, detail=u.detail, queued_at=now
            )
            for u in status.units
        ],
        server_now=now,
    )


@router.get("/{run_id}", response_model=RunView)
def get_run(
    run_id: str,
    conn: psycopg2.extensions.connection = Depends(get_db_conn),
) -> RunView:
    from reimagina.orchestration.factory import get_orchestrator

    status = get_orchestrator().get_run(conn, run_id)
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


async def _event_stream(run_id: str) -> AsyncIterator[str]:
    """Frame the orchestrator's progress events as SSE, with route-owned policy.

    The orchestrator decides *what* happened; this decides how it reaches the
    wire — frame format, keep-alive cadence, and the lifetime cap. Where the
    events come from (LISTEN/NOTIFY, a workflow stream, a websocket) is not this
    module's business.
    """
    import reimagina.orchestration.frames as frames
    from reimagina.orchestration.factory import get_orchestrator

    events = get_orchestrator().subscribe_progress(run_id).__aiter__()
    deadline = time.monotonic() + _STREAM_MAX_SECONDS
    # Hold one pending __anext__ across heartbeats rather than re-awaiting it.
    # Wrapping the await in a timeout would cancel it mid-flight, which leaves
    # an async generator unresumable — the keep-alive would break the stream it
    # exists to keep open.
    pending = asyncio.ensure_future(anext(events))
    try:
        while time.monotonic() < deadline:
            done, _ = await asyncio.wait({pending}, timeout=_HEARTBEAT_SECONDS)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.ensure_future(anext(events))
            yield frames.event_frame(event)
            if event.indicator is None and frames.is_run_terminal(event.status):
                return
    finally:
        pending.cancel()
        # Let the cancellation land inside the generator so its own cleanup runs
        # before aclose() — closing one still suspended raises instead.
        with contextlib.suppress(BaseException):
            await pending
        # `subscribe_progress` is typed AsyncIterator, which has no `aclose`;
        # the concrete generator does, and closing it is what releases the
        # progress-stream slot.
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


@router.get("/{run_id}/events")
def stream_run_events(run_id: str) -> StreamingResponse:
    """404 an unknown run, then stream. Owns its connection for the check only.

    ⚠ **Not** ``Depends(get_db_conn)``, which is what this used to take. FastAPI
    closes a ``yield`` dependency after the *response completes*, and a
    ``StreamingResponse`` does not complete until its body iterator is
    exhausted — so a pool connection borrowed here stayed borrowed for the whole
    subscription, up to ``_STREAM_MAX_SECONDS``. With ``REIMAGINA_API_POOL_MAX``
    at 16, seventeen concurrent subscribers starved every other route in the
    process for five minutes, for a connection this route stops using the moment
    the check returns.

    A short-lived connection of its own instead, closed before streaming starts,
    exactly as ``submit_run`` and ``health._probe_db`` do. The progress stream
    itself needs no connection from us: ``subscribe_progress`` takes none,
    because no engine's progress transport is the caller's pooled one.
    """
    from reimagina.db import connect
    from reimagina.orchestration.factory import get_orchestrator

    with contextlib.closing(connect(api_dsn())) as conn:
        found = get_orchestrator().get_run(conn, run_id) is not None
    if not found:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return StreamingResponse(_event_stream(run_id), media_type="text/event-stream")
