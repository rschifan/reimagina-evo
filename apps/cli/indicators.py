"""CLI helpers for the indicator layer."""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import asdict, is_dataclass

from reimagina.db import connect, managed_connection
from reimagina.indicators.catalog import seed_catalog
from reimagina.jobs.aoi_summary import summarise_aoi
from reimagina.jobs.rollup import run_rollup
from reimagina.orchestration.intake import evaluate_scenario
from reimagina.orchestration.runners import default_runners


def seed_indicator_catalog(*, dsn: str) -> dict[str, object]:
    """Idempotently register every shipped indicator in indicator_catalog."""
    with managed_connection(dsn) as conn:
        seeded = seed_catalog(conn)
    return {"dsn": dsn, "seeded": seeded, "count": len(seeded)}


def compute_indicator(
    *,
    dsn: str,
    slug: str,
    aoi_id: str | None = None,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, object]:
    """Dispatch to the registered runner for `slug`.

    Delegates to ``default_runners()`` rather than an if/elif per slug, so every
    registered indicator — direct or composite — is reachable from the CLI the
    moment it lands in the catalog. The previous hardcoded pair silently omitted
    ``urban_form_block_planned_floors``.
    """
    runners = default_runners()
    runner = runners.get(slug)
    if runner is None:
        known = ", ".join(sorted(runners))
        raise ValueError(f"Unknown indicator slug: {slug!r}. Known slugs: {known}.")

    with managed_connection(dsn) as conn:
        # Empty feature set = AOI-scoped (or whole-grain) compute, which is what
        # the CLI has always done; the dirty-set path passes a real scope.
        result = runner(
            conn,
            features=frozenset(),
            scenario_id=scenario_id,
            code_version=code_version,
            aoi_id=aoi_id,
        )
    payload = asdict(result) if is_dataclass(result) else {"result": repr(result)}
    return {"dsn": dsn, **payload}


def rollup_indicator(
    *,
    dsn: str,
    slug: str,
    source_feature_type: str,
    target_feature_type: str,
    scenario_id: str | None = None,
    code_version: str = "dev",
) -> dict[str, object]:
    """Roll an indicator up from a source grain to a coarser target grain."""
    with managed_connection(dsn) as conn:
        result = run_rollup(
            conn,
            indicator_slug=slug,
            source_feature_type=source_feature_type,
            target_feature_type=target_feature_type,
            scenario_id=scenario_id,
            code_version=code_version,
        )
    return {
        "dsn": dsn,
        "indicator_slug": result.indicator_slug,
        "job_run_id": result.job_run_id,
        "source_feature_type": result.source_feature_type,
        "target_feature_type": result.target_feature_type,
        "aggregation_method": result.aggregation_method,
        "targets_considered": result.targets_considered,
        "rows_written": result.rows_written,
    }


def summarise_aoi_indicator(
    *,
    dsn: str,
    aoi_id: str,
    slug: str,
    scenario_id: str | None = None,
) -> dict[str, object]:
    """Compute and upsert one aoi_indicator_summary row."""
    with managed_connection(dsn) as conn:
        result = summarise_aoi(
            conn,
            aoi_id=aoi_id,
            indicator_slug=slug,
            scenario_id=scenario_id,
        )
    return {
        "dsn": dsn,
        "aoi_id": result.aoi_id,
        "indicator_slug": result.indicator_slug,
        "scenario_id": result.scenario_id,
        "value": result.value,
        "status": result.status,
        "aggregation_note": result.aggregation_note,
    }


def evaluate_scenario_cli(
    *,
    dsn: str,
    changed_attributes: list[str],
    edited_features: list[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    mode: str = "embedded",
) -> dict[str, object]:
    """Selectively recompute indicators for a scenario edit.

    ``mode`` selects the orchestrator:

    * ``embedded`` (default) — submit to the engine and **block until the run is
      terminal**, returning the finished report.

      ⚠ On this branch "embedded" no longer means what the name says. There is
      no in-process drain, because there is no in-process execution: a standing
      worker must already be running or this waits forever. It is kept as the
      default so the CLI's shape matches the other branch, and because "run it
      and tell me when it's done" is still what a caller wants — but the work
      happens in another process. On the library track the same flag genuinely
      does drain the run in-process. That difference is a comparison row.
    * ``detach`` — submit and return the run_id immediately.
    * ``sync`` — the in-process orchestrator, no engine and no worker. On this
      branch it is the *only* path that computes anything without a running
      Hatchet.

    ``embedded`` and ``detach`` need a reachable engine (``HATCHET_CLIENT_TOKEN``);
    the worker they depend on needs ``DATABASE_URL_DIRECT``.
    """
    if mode == "detach":
        from reimagina.orchestration.factory import get_orchestrator

        with closing(connect(dsn)) as conn:
            with conn:
                handle = get_orchestrator().submit_run(
                    conn,
                    changed_attributes=changed_attributes,
                    edited_features=edited_features,
                    scenario_id=scenario_id,
                    aoi_id=aoi_id,
                    code_version=code_version,
                )
        status = handle.status.status if handle.status else "queued"
        return {
            "dsn": dsn,
            # No run row exists for a noop, so the id would 404 on lookup.
            "run_id": None if status == "noop" else handle.run_id,
            "status": status,
            "mode": "detach",
        }

    if mode == "sync":
        # Not via the factory: this path returns the coordinator's per-unit
        # RunReport, which is a richer shape than the port's RunStatus and is
        # what the CLI has always printed.
        with closing(connect(dsn)) as conn:
            with conn:
                report = evaluate_scenario(
                    conn,
                    changed_attributes=changed_attributes,
                    edited_features=edited_features,
                    scenario_id=scenario_id,
                    aoi_id=aoi_id,
                    code_version=code_version,
                )
        return {
            "dsn": dsn,
            "mode": "sync",
            "status": report.status,
            "outcomes": [
                {"indicator": o.indicator, "status": o.status.value, "detail": o.detail}
                for o in report.outcomes
            ],
        }

    if mode != "embedded":
        raise ValueError(f"unknown evaluate-scenario mode: {mode!r}")

    import time

    from reimagina.orchestration.factory import get_orchestrator
    from reimagina.orchestration.port import RUN_TERMINAL_STATES

    orchestrator = get_orchestrator()
    with closing(connect(dsn)) as conn:
        with conn:
            handle = orchestrator.submit_run(
                conn,
                changed_attributes=changed_attributes,
                edited_features=edited_features,
                scenario_id=scenario_id,
                aoi_id=aoi_id,
                code_version=code_version,
            )
    if handle.status and handle.status.status == "noop":
        return {
            "dsn": dsn,
            "run_id": None,
            "mode": "embedded",
            "status": "noop",
            "units": [],
        }

    # Poll rather than `subscribe_progress`: this is a synchronous CLI entry
    # point, and spinning up an event loop to consume an async iterator we only
    # want the *last* value of buys nothing. Hatchet owns status, so do not hold
    # the planning connection open for the potentially long wait.
    deadline = time.monotonic() + _EMBEDDED_TIMEOUT_S
    status = None
    while time.monotonic() < deadline:
        status = orchestrator.get_run(None, handle.run_id)
        if status and status.status in RUN_TERMINAL_STATES:
            break
        time.sleep(1.0)
    else:
        # ⚠ Almost always "no worker is running", not "the run is slow" —
        # nothing executes on this branch without one, and a run with no worker
        # sits QUEUED indefinitely rather than failing.
        raise TimeoutError(
            f"run {handle.run_id} did not finish within "
            f"{_EMBEDDED_TIMEOUT_S}s. Is a worker running "
            f"(`python -m apps.worker`)?"
        )

    return {
        "dsn": dsn,
        "run_id": handle.run_id,
        "mode": "embedded",
        "status": status.status if status else "unknown",
        "units": [
            {"indicator": u.indicator, "status": u.status, "detail": u.detail}
            for u in (status.units if status else ())
        ],
    }


_EMBEDDED_TIMEOUT_S = float(os.environ.get("REIMAGINA_EMBEDDED_TIMEOUT_S", "3600"))
