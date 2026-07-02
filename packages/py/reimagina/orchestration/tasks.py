"""Procrastinate tasks — the async distributor (Phase 3, components 6/7/8).

``compute_unit`` runs ONE indicator's runner for a run in its own committed
transaction; ``advance_run`` walks the dirty-set readiness and enqueues the next
wave, finalising the run when every unit is terminal. Wave ordering and
partial-success stay in the domain readiness rule (``coordinator._dep_map`` +
the "all dirty inputs succeeded" test); Procrastinate only moves units between
workers with durability, retries, and queueing locks (dedup).
"""

from __future__ import annotations

from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from reimagina.db import connect
from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.orchestration import runstore
from reimagina.orchestration.coordinator import plan_wave
from reimagina.orchestration.queue_app import app, queue_dsn
from reimagina.orchestration.runners import default_runners


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
            runstore.set_unit_status(
                conn,
                run_id=run_id,
                indicator_slug=indicator_slug,
                status="skipped",
                detail="no runner registered",
            )
        else:
            try:
                runner(
                    conn,
                    features=frozenset(feature_scope),
                    scenario_id=scenario_id,
                    code_version=code_version,
                    aoi_id=aoi_id,
                )
                runstore.set_unit_status(
                    conn,
                    run_id=run_id,
                    indicator_slug=indicator_slug,
                    status="succeeded",
                )
            except Exception as exc:  # noqa: BLE001 — deterministic failure = terminal
                conn.rollback()
                runstore.set_unit_status(
                    conn,
                    run_id=run_id,
                    indicator_slug=indicator_slug,
                    status="failed",
                    detail=str(exc),
                )
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

        for slug in plan.skip:
            runstore.set_unit_status(
                conn,
                run_id=run_id,
                indicator_slug=slug,
                status="skipped",
                detail="unsatisfied inputs",
            )
        for slug in plan.dispatch:
            runstore.set_unit_status(
                conn, run_id=run_id, indicator_slug=slug, status="running"
            )
            dispatch_features[slug] = features_by[slug]
        if plan.final_status is not None:
            runstore.finalize_run(conn, run_id=run_id, status=plan.final_status)
        conn.commit()

    for slug, features in dispatch_features.items():
        _defer_unit(run_id, slug, features, params)
