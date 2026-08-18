"""CLI helpers for the indicator layer."""

from __future__ import annotations

import time
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reimagina.orchestration.port import Orchestrator

from reimagina.db import connect
from reimagina.indicators.catalog import seed_catalog
from reimagina.jobs.aoi_summary import summarise_aoi
from reimagina.jobs.rollup import run_rollup
from reimagina.orchestration.intake import evaluate_scenario
from reimagina.orchestration.runners import default_runners


def seed_indicator_catalog(*, dsn: str) -> dict[str, object]:
    """Idempotently register every shipped indicator in indicator_catalog."""
    with connect(dsn) as conn:
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

    with connect(dsn) as conn:
        # Empty feature set = AOI-scoped (or whole-grain) compute, which is what
        # the CLI has always done; the dirty-set path passes a real scope.
        result = runner(
            conn,
            features=frozenset(),
            scenario_id=scenario_id,
            code_version=code_version,
            aoi_id=aoi_id,
        )
    # `is_dataclass` is true for the class object too, which `asdict` rejects.
    payload = (
        asdict(result)
        if is_dataclass(result) and not isinstance(result, type)
        else {"result": repr(result)}
    )
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
    with connect(dsn) as conn:
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
    with connect(dsn) as conn:
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


def submit_after_ingest(
    *,
    dsn: str,
    changed_attributes: list[str] | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    mode: str = "detach",
) -> dict[str, object]:
    """Recompute the indicators a completed ingestion invalidated.

    With ``changed_attributes`` omitted — now the default — the dirty set comes
    from **freshness**: the load published a new version for the layer it wrote,
    so every indicator consuming that layer is out of date and says so on its
    own. Nothing has to name an attribute standing in for the layer.

    That default replaces a guess. The caller used to pass ``("geometry",)``
    because writing building footprints changes geometry, which was true but
    far too broad: it named an attribute three indicators read, so reloading
    buildings also recomputed compactness, which never looks at a building. The
    layer edge is exact where the attribute name was a proxy.

    Passing ``changed_attributes`` explicitly still works and still takes
    precedence, for a caller that genuinely knows something the graph does not.

    ⚠ **Deliberately not feature-scoped**, and this is the load-bearing decision
    in the function. A loader knows the ids of the rows *it wrote*, and passing
    those would look like a tighter recompute while being wrong: an Overture
    load writes **buildings**, whereas every shipped indicator declares
    ``applies_to=("block",)``. Scoping to building ids would hand the compute
    functions a feature set none of their rows match — a recompute that touches
    nothing and reports success, leaving the indicator stale with no error to
    show for it.

    ``impact.DirtyUnit`` already defines the empty set as "every feature this
    indicator applies to", which is the correct reading of "new source data
    landed": broader than necessary, never wrong.

    # ponytail: full-extent recompute per affected indicator; narrow it when
    # `impact.FeatureExpander` grows a real implementation. That hook exists for
    # exactly this — mapping the features a change touched onto the features
    # whose value changes (building → containing block, child → parent grain).
    # It needs the spatial feature graph, which is not derivable from the
    # registry, so it is injected rather than inferred. Until it lands, narrowing
    # here would be guessing at a join the engine cannot check.
    """
    from reimagina.orchestration.factory import get_orchestrator

    with connect(dsn) as conn:
        handle = get_orchestrator(mode).submit_run(
            conn,
            changed_attributes=changed_attributes or (),
            edited_features=(),
            scenario_id=None,  # an ingest changes the baseline, not a scenario
            aoi_id=aoi_id,
            code_version=code_version,
        )
    status = handle.status.status if handle.status else "queued"
    return {
        "run_id": None if status == "noop" else handle.run_id,
        "status": status,
        "mode": mode,
        "changed_attributes": list(changed_attributes or ()),
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
    timeout_s: float = 600.0,
) -> dict[str, object]:
    """Selectively recompute indicators for a scenario edit.

    ``mode`` selects whether this command waits, not which engine runs the work —
    there is one ops-plane, and because it is a library rather than a service the
    same process can both submit and drain. (``orchestration.factory`` uses the
    words ``detach``/``embedded`` for engines and rejects them; here they name
    caller behaviour. Same words, different layer.)

    * ``embedded`` (default) — submit and wait for the run to finish, returning
      the per-unit report. No standing worker needed; this process drives it.
    * ``detach`` — submit and return the run_id for a ``python -m apps.worker``
      fleet to drain (fire-and-forget).
    * ``sync`` — no ops-plane at all: run the coordinator in-process and return
      its richer per-unit report. The one mode that still works when the
      ops-plane is broken.

    ``embedded`` and ``detach`` need ``DATABASE_URL_DIRECT``.
    """
    if mode == "sync":
        # Not via the factory: this path returns the coordinator's per-unit
        # RunReport, which is a richer shape than the port's RunStatus and is
        # what the CLI has always printed.
        with connect(dsn) as conn:
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

    if mode not in ("embedded", "detach"):
        raise ValueError(f"unknown evaluate-scenario mode: {mode!r}")

    from reimagina.orchestration.factory import get_orchestrator

    if mode == "embedded":
        # Local convenience mode deliberately owns an executor. Production API
        # processes stay client-only; this is an explicit caller choice.
        from reimagina.orchestration.dbos_orchestrator import DBOSOrchestrator

        orchestrator: Orchestrator = DBOSOrchestrator(execution_mode="dev")
    else:
        orchestrator = get_orchestrator("dbos")
    with connect(dsn) as conn:
        handle = orchestrator.submit_run(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
        )
        # A noop finishes at submit and persists no run, so there is nothing to
        # wait for and the id would 404 on lookup.
        if handle.status is not None or mode == "detach":
            status = handle.status.status if handle.status else "queued"
            return {
                "dsn": dsn,
                "run_id": None if status == "noop" else handle.run_id,
                "status": status,
                "mode": mode,
            }
        final = _await_run(orchestrator, conn, handle.run_id, timeout_s=timeout_s)

    return {
        "dsn": dsn,
        "run_id": final.run_id,
        "mode": "embedded",
        "status": final.status,
        "units": [
            {"indicator": u.indicator, "status": u.status, "detail": u.detail}
            for u in final.units
        ],
    }


def _await_run(orchestrator, conn, run_id: str, *, timeout_s: float):
    """Poll until the run is terminal.

    The port has no ``wait``: an ops-plane that required a separate worker could
    not honour one, so blocking belongs to the caller that wants it. This
    process launched the engine, so the run makes progress while we poll.
    """
    from reimagina.orchestration.port import RUN_TERMINAL_STATES

    deadline = time.monotonic() + timeout_s
    while True:
        conn.rollback()  # end the read txn so the next poll sees fresh rows
        status = orchestrator.get_run(conn, run_id)
        if status is not None and status.status in RUN_TERMINAL_STATES:
            return status
        if time.monotonic() > deadline:
            last = status.status if status else "unknown"
            raise TimeoutError(
                f"run {run_id} did not finish within {timeout_s}s (last: {last})"
            )
        time.sleep(0.2)
