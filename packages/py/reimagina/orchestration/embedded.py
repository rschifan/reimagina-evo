"""Embedded-drain orchestrator — the interactive default.

Submits a run to the Procrastinate queue, then drains a *bounded in-process
worker* until the run finalizes, and returns the final :class:`RunStatus`. Same
durable queue path as the detached worker (retries, queueing-lock dedup, wave
dispatch), but without a separate ``python -m apps.worker`` process — the call
returns the finished report, the way the old synchronous path did.

Only ``submit_run`` differs from the detached orchestrator; reading, cancelling
and subscribing are the same queue and the same tables, so they delegate rather
than fork.

Requires ``DATABASE_URL_DIRECT`` (the worker tasks open their own domain
connections through it, see ``tasks.py``); there is no pooled-DSN fallback.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable, Sequence

from reimagina.indicators.catalog import SHIPPED_INDICATORS, IndicatorSpec
from reimagina.orchestration.port import (
    RUN_TERMINAL_STATES,
    RunEvent,
    RunHandle,
    RunStatus,
)
from reimagina.orchestration.procrastinate_orchestrator import ProcrastinateOrchestrator
from reimagina.orchestration.queue_app import app, bound

# Statuses at which the run is done and the drain loop can stop. ``noop`` is the
# return-only status submit_run hands back for an empty dirty-set — never
# persisted, never drained, so it is not in the shared run vocabulary.
_TERMINAL = RUN_TERMINAL_STATES | {"noop"}


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


class EmbeddedOrchestrator:
    """Port implementation: enqueue durably, then drain it here and return."""

    def __init__(
        self,
        *,
        registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS,
        concurrency: int = 1,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        self._inner = ProcrastinateOrchestrator(registry=registry)
        self._concurrency = concurrency
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    def submit_run(
        self,
        conn,
        *,
        changed_attributes: Iterable[str],
        edited_features: Iterable[str],
        scenario_id: str | None = None,
        aoi_id: str | None = None,
        code_version: str = "dev",
    ) -> RunHandle:
        """Submit and drain to completion; the handle carries the final status.

        Raises ``TimeoutError`` if the run has not finalized within
        ``timeout_s`` (a wedged unit, or a retry backoff longer than the budget).
        """
        # Bind submit + drain to one connector so the deferred jobs and the
        # worker that runs them share a broker (robust vs import-time env
        # capture). `bound()` also serializes the swap — it mutates a
        # module-global, and two concurrent `wait: true` requests would
        # otherwise interleave their save/restore.
        with bound():
            handle = self._inner.submit_run(
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
                return handle

            run_id = handle.run_id
            start = time.monotonic()
            while True:
                _drain_once(self._concurrency)
                # The worker committed on its own connection; end any read txn
                # on the caller's connection so the next poll sees fresh rows.
                conn.rollback()
                status = self._inner.get_run(conn, run_id)
                if status is not None and status.status in _TERMINAL:
                    return RunHandle(run_id=run_id, status=status)
                if time.monotonic() - start > self._timeout_s:
                    last = status.status if status else "unknown"
                    raise TimeoutError(
                        f"embedded run {run_id} did not finalize within "
                        f"{self._timeout_s}s (last status: {last})"
                    )
                time.sleep(self._poll_interval_s)

    def get_run(self, conn, run_id: str) -> RunStatus | None:
        return self._inner.get_run(conn, run_id)

    def cancel_run(self, conn, run_id: str) -> bool:
        return self._inner.cancel_run(conn, run_id)

    def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]:
        return self._inner.subscribe_progress(run_id)


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

    The function form of :class:`EmbeddedOrchestrator`, kept because the CLI and
    the integration tests want a terminal :class:`RunStatus` rather than a
    handle. New callers should go through ``factory.get_orchestrator``.
    """
    handle = EmbeddedOrchestrator(
        registry=registry,
        concurrency=concurrency,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    ).submit_run(
        conn,
        changed_attributes=changed_attributes,
        edited_features=edited_features,
        scenario_id=scenario_id,
        aoi_id=aoi_id,
        code_version=code_version,
    )
    assert handle.status is not None  # embedded always drains to a terminal status
    return handle.status
