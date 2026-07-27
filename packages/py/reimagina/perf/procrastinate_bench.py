"""Timing harness for the Procrastinate ops-plane.

Measures the queue's *own* cost — enqueue latency, drain throughput at varying
worker concurrency, and the fixed per-job overhead — isolated from domain
compute via a no-op task. This is not an EXPLAIN/plan check (see
``perf/manifest.py`` for those), so it does not use ``BenchmarkSpec``; it reuses
only ``LatencyBudget``'s p50/p95/p99 shape for reporting.

Requires ``DATABASE_URL_DIRECT``. Standalone: manages the connector binding and
registers its own no-op task, so it does not touch the domain schema.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from procrastinate import PsycopgConnector

from reimagina.orchestration.queue_app import _SEARCH_PATH, app, queue_dsn
from reimagina.perf.manifest import LatencyBudget


@app.task(name="_bench_noop", queue="compute")
def _bench_noop(*, i: int = 0) -> None:
    """A no-op unit of work — measures the queue overhead, not the payload."""


@dataclass(frozen=True)
class ProcrastinateBenchReport:
    enqueue_count: int
    submit_latency_ms: LatencyBudget
    submit_min_ms: float
    submit_max_ms: float
    # concurrency -> units/second draining ``enqueue_count`` no-op jobs
    throughput_units_per_s: dict[int, float]
    # serial per-job overhead (drain wall-time / N at concurrency=1), ms
    per_job_overhead_ms: float


def _percentile(sorted_samples: list[float], pct: float) -> float:
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    k = min(n - 1, max(0, round((pct / 100.0) * (n - 1))))
    return sorted_samples[k]


def _drain(concurrency: int) -> None:
    """Drain the compute queue to empty with the given worker concurrency."""

    async def _run() -> None:
        async with app.open_async():
            await app.run_worker_async(
                queues=["compute"],
                wait=False,
                listen_notify=False,
                concurrency=concurrency,
                install_signal_handlers=False,
            )

    asyncio.run(_run())


def _clear_jobs(conn) -> None:
    with conn.cursor() as cur:
        # The delete trigger touches unqualified procrastinate tables.
        cur.execute("SET search_path TO procrastinate, public")
        cur.execute("DELETE FROM procrastinate.procrastinate_jobs")
        cur.execute("SET search_path TO public")
    if not getattr(conn, "autocommit", False):
        conn.commit()


def _enqueue(n: int) -> None:
    with app.open():
        for i in range(n):
            _bench_noop.defer(i=i)


def run_benchmarks(
    conn,
    *,
    enqueue_count: int = 200,
    concurrencies: tuple[int, ...] = (1, 4, 8),
) -> ProcrastinateBenchReport:
    """Run the queue benchmarks and return a structured report.

    ``conn`` is any Postgres connection on the same database (used only to clear
    jobs between phases). The queue itself runs on ``DATABASE_URL_DIRECT``.
    """
    queue_dsn()  # fail loudly if DATABASE_URL_DIRECT is unset
    connector = PsycopgConnector(conninfo=queue_dsn(), kwargs={"options": _SEARCH_PATH})

    with app.replace_connector(connector):
        # 1) Enqueue latency — time each defer, then drain to clear.
        _clear_jobs(conn)
        samples: list[float] = []
        with app.open():
            for i in range(enqueue_count):
                t0 = time.perf_counter()
                _bench_noop.defer(i=i)
                samples.append((time.perf_counter() - t0) * 1000.0)
        _drain(1)
        samples.sort()
        submit = LatencyBudget(
            p50_ms=_percentile(samples, 50),
            p95_ms=_percentile(samples, 95),
            p99_ms=_percentile(samples, 99),
        )

        # 2) Throughput at each concurrency — enqueue N, time the drain.
        throughput: dict[int, float] = {}
        per_job_ms = 0.0
        for c in concurrencies:
            _clear_jobs(conn)
            _enqueue(enqueue_count)
            t0 = time.perf_counter()
            _drain(c)
            dt = time.perf_counter() - t0
            throughput[c] = enqueue_count / dt if dt > 0 else float("inf")
            if c == 1:
                per_job_ms = (dt / enqueue_count) * 1000.0

    _clear_jobs(conn)
    return ProcrastinateBenchReport(
        enqueue_count=enqueue_count,
        submit_latency_ms=submit,
        submit_min_ms=samples[0],
        submit_max_ms=samples[-1],
        throughput_units_per_s=throughput,
        per_job_overhead_ms=per_job_ms,
    )


def format_report(report: ProcrastinateBenchReport) -> str:
    """Render a compact human-readable table."""
    lines = [
        f"Procrastinate queue benchmark (N={report.enqueue_count} no-op jobs)",
        "  enqueue latency (ms):  "
        f"p50={report.submit_latency_ms.p50_ms:.2f}  "
        f"p95={report.submit_latency_ms.p95_ms:.2f}  "
        f"p99={report.submit_latency_ms.p99_ms:.2f}  "
        f"(min={report.submit_min_ms:.2f} max={report.submit_max_ms:.2f})",
        f"  serial per-job overhead: {report.per_job_overhead_ms:.2f} ms",
        "  drain throughput (units/s):",
    ]
    for c in sorted(report.throughput_units_per_s):
        lines.append(f"    concurrency={c:<3d} {report.throughput_units_per_s[c]:8.1f}")
    return "\n".join(lines)
