"""Procrastinate tasks — the async distributor (Phase 3, components 6/7/8).

``compute_unit`` runs ONE indicator's runner for a run in its own committed
transaction; ``advance_run`` walks the dirty-set readiness and enqueues the next
wave, finalising the run when every unit is terminal. Wave ordering and
partial-success stay in the domain readiness rule (``coordinator._dep_map`` +
the "all dirty inputs succeeded" test); Procrastinate only moves units between
workers with durability, retries, and queueing locks (dedup).
"""

from __future__ import annotations

import logging

from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from reimagina.db import connect
from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.jobs._lifecycle import current_run
from reimagina.orchestration import events, runstore
from reimagina.orchestration.wave import plan_wave
from reimagina.orchestration.queue_app import app, queue_dsn
from reimagina.orchestration.runners import default_runners
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)


def _defer_unit(run_id: str, slug: str, features, params: dict) -> None:
    try:
        compute_unit.configure(queueing_lock=f"unit:{run_id}:{slug}").defer(
            run_id=run_id,
            indicator_slug=slug,
            feature_scope=list(features),
            scenario_id=params.get("scenario_id"),
            aoi_id=params.get("aoi_id"),
            code_version=params.get("code_version", "dev"),
        )
    except AlreadyEnqueued:
        pass  # a run of this unit is already queued


def _defer_advance(run_id: str) -> None:
    try:
        advance_run.configure(queueing_lock=f"advance:{run_id}").defer(run_id=run_id)
    except AlreadyEnqueued:
        pass  # an advance for this run is already queued; it'll see fresh state


def _record_failed_job_run(
    *,
    run_id: str,
    indicator_slug: str,
    scenario_id: str | None,
    code_version: str,
    error: str,
) -> None:
    """Persist a durable ``failed`` job_run on its own connection.

    ``compute_unit`` rolls back the shared connection on a runner exception,
    which erases the job_run the runner opened — so a failure's provenance must
    be recorded independently. Best-effort: a failure here must never mask the
    unit failure it is trying to record.

    ``run_id`` is passed rather than taken from the ambient ``current_run``: this
    runs from an ``except`` block, and the ambient scope has already closed by
    then. Leaving it to the ambient would file every failed unit under a NULL
    run — invisible to `cancel_run_backends` and to the per-run measurement
    query, which are the two things `run_id` exists for.
    """
    spec = next((s for s in SHIPPED_INDICATORS if s.slug == indicator_slug), None)
    model_family = spec.model_family if spec and spec.model_family else "unknown"
    try:
        with connect(queue_dsn()) as conn:
            repo = backend_for_connection(conn).job_run_repo()
            job_run_id = repo.start_job(
                model_family=model_family,
                code_version=code_version,
                scenario_id=scenario_id,
                status="running",
                run_id=run_id,
            )
            repo.complete_job(job_run_id, status="failed", error_message=error)
            conn.commit()
    except Exception:  # noqa: BLE001 — provenance is best-effort
        logger.warning(
            "could not persist failed job_run for %s", indicator_slug, exc_info=True
        )


@app.task(queue="compute", retry=RetryStrategy(max_attempts=3, exponential_wait=2))
def compute_unit(
    *,
    run_id: str,
    indicator_slug: str,
    feature_scope: list[str],
    scenario_id: str | None,
    aoi_id: str | None,
    code_version: str,
) -> None:
    """Run one indicator's runner, record the outcome, then advance the run.

    A failed ``connect`` (transient infra) propagates → Procrastinate retries.
    A runner exception is deterministic → recorded as a terminal ``failed`` (no
    retry), and the run still advances so dependents skip.
    """
    runner = default_runners().get(indicator_slug)
    with connect(queue_dsn()) as conn:
        if runner is None:
            status, detail = "skipped", "no runner registered"
        else:
            try:
                # Set here, inside the worker thread that runs the compute — a
                # ContextVar set by the dispatcher would not be visible here.
                with current_run(run_id):
                    runner(
                        conn,
                        features=frozenset(feature_scope),
                        scenario_id=scenario_id,
                        code_version=code_version,
                        aoi_id=aoi_id,
                    )
                status, detail = "succeeded", None
            except Exception as exc:  # noqa: BLE001 — deterministic failure = terminal
                conn.rollback()
                status, detail = "failed", str(exc)
                # The rollback above erased the runner's job_run row; record the
                # failure durably on a separate connection so provenance survives.
                _record_failed_job_run(
                    run_id=run_id,
                    indicator_slug=indicator_slug,
                    scenario_id=scenario_id,
                    code_version=code_version,
                    error=str(exc),
                )
        # Announce only what the database accepted. A cancelled run has already
        # written this unit's terminal status; publishing our own on top would
        # tell every SSE subscriber `failed` for a unit GET reports `cancelled`.
        if runstore.set_unit_status(
            conn,
            run_id=run_id,
            indicator_slug=indicator_slug,
            status=status,
            detail=detail,
        ):
            events.emit(conn, run_id=run_id, indicator=indicator_slug, status=status)
        conn.commit()
    _defer_advance(run_id)


@app.task(queue="control")
def advance_run(*, run_id: str) -> None:
    """Dispatch the next ready wave; finalise the run when all units terminal."""
    dispatch_features: dict[str, list[str]] = {}
    with connect(queue_dsn()) as conn:
        units = runstore.list_units(conn, run_id)
        params = runstore.run_params(conn, run_id) or {}
        plan = plan_wave(
            [(u["indicator"], u["status"]) for u in units], SHIPPED_INDICATORS
        )
        features_by = {u["indicator"]: u["features"] for u in units}

        # Every write below is announced only if it landed — a cancelled run has
        # already made its units terminal, and re-announcing over the top would
        # reopen a stream the cancel closed. Not dispatching a refused unit is
        # the load-bearing half: it is what stops a cancelled run from starting
        # more compute.
        for slug in plan.skip:
            if runstore.set_unit_status(
                conn,
                run_id=run_id,
                indicator_slug=slug,
                status="skipped",
                detail="unsatisfied inputs",
            ):
                events.emit(conn, run_id=run_id, indicator=slug, status="skipped")
        for slug in plan.dispatch:
            if not runstore.set_unit_status(
                conn, run_id=run_id, indicator_slug=slug, status="running"
            ):
                continue
            events.emit(conn, run_id=run_id, indicator=slug, status="running")
            dispatch_features[slug] = features_by[slug]
        if plan.final_status is not None and runstore.finalize_run(
            conn, run_id=run_id, status=plan.final_status
        ):
            events.emit(conn, run_id=run_id, indicator=None, status=plan.final_status)
        conn.commit()

    for slug, features in dispatch_features.items():
        _defer_unit(run_id, slug, features, params)
