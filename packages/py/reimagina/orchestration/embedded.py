"""Embedded-drain orchestrator — the interactive default.

Submits a run to the Procrastinate queue, then drains a *bounded in-process
worker* until the run finalizes, and returns the final :class:`RunStatus`. Same
durable queue path as the detached worker (retries, queueing-lock dedup, wave
dispatch), but without a separate ``python -m apps.worker`` process — the call
returns the finished report, the way the old synchronous path did.

Requires ``DATABASE_URL_DIRECT`` (the worker tasks open their own domain
connections through it, see ``tasks.py``); there is no pooled-DSN fallback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence

from procrastinate import PsycopgConnector

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.port import RunStatus
from reimagina.orchestration.procrastinate_orchestrator import ProcrastinateOrchestrator
from reimagina.orchestration.queue_app import _SEARCH_PATH, app, queue_dsn

# Statuses at which the run is done and the drain loop can stop. ``noop`` is the
# return-only status submit_run hands back for an empty dirty-set (never drained).
_TERMINAL = frozenset({"succeeded", "partial", "failed", "noop"})


def _drain_once(concurrency: int) -> None:
    """Run a bounded worker that processes every immediately-available job then
    exits (``wait=False``). One call drains a whole no-failure wave cascade;
    delayed retries need the caller's outer loop to drain again once due."""

    async def _run() -> None:
        async with app.open_async():
            await app.run_worker_async(
                wait=False,
                listen_notify=False,
                concurrency=concurrency,
                install_signal_handlers=False,
            )

    asyncio.run(_run())


def run_embedded(
    conn,
    *,
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
    scenario_id: str | None = None,
    aoi_id: str | None = None,
    code_version: str = "dev",
    registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
    concurrency: int = 1,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.1,
) -> RunStatus:
    """Submit a run and drain an in-process worker until it finalizes.

    Returns the terminal :class:`RunStatus`. Raises ``TimeoutError`` if the run
    has not finalized within ``timeout_s`` (a wedged unit or a retry backoff
    longer than the budget).
    """
    dsn = queue_dsn()  # fail loudly if DATABASE_URL_DIRECT is unset
    orchestrator = ProcrastinateOrchestrator(registry=registry)
    connector = PsycopgConnector(conninfo=dsn, kwargs={"options": _SEARCH_PATH})

    # Bind submit + drain to one connector so the deferred jobs and the worker
    # that runs them share a broker (robust vs import-time env capture).
    with app.replace_connector(connector):
        handle = orchestrator.submit_run(
            conn,
            changed_attributes=changed_attributes,
            edited_features=edited_features,
            scenario_id=scenario_id,
            aoi_id=aoi_id,
            code_version=code_version,
        )
        # Empty dirty-set → submit_run returns a populated (noop) status and
        # enqueues nothing; there is nothing to drain.
        if handle.status is not None:
            return handle.status

        run_id = handle.run_id
        start = time.monotonic()
        while True:
            _drain_once(concurrency)
            # The worker committed on its own connection; end any read txn on
            # the caller's connection so the next poll sees fresh rows.
            conn.rollback()
            status = orchestrator.get_run(conn, run_id)
            if status is not None and status.status in _TERMINAL:
                return status
            if time.monotonic() - start > timeout_s:
                last = status.status if status else "unknown"
                raise TimeoutError(
                    f"embedded run {run_id} did not finalize within "
                    f"{timeout_s}s (last status: {last})"
                )
            time.sleep(poll_interval_s)
