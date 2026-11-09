"""Postgres-native executor liveness, deliberately separate from recovery.

Separate, because DBOS has no opinion about whether an executor is alive — it
recovers an executor's work only when that executor restarts and says so. The
table here is the signal that lets a *different* process decide an executor is
never coming back. ``orchestration.reclaim`` is what reads it.

Every process that calls ``dbos_app.launch()`` publishes here, not only the
worker. That is not symmetry for its own sake: ``launch()`` registers the
queues with a worker concurrency, so any process that gets that far can
dequeue and execute units. The worker is the obvious one; ``dev`` mode is the
easy one to forget, and it is how the CLI's ``evaluate-scenario`` and the
integration suite execute — in the submitting process, under its own executor
id. A ``client`` never launches at all, which is the API's default. An
executor that can own work and does not announce itself is one whose
abandoned work can never be reclaimed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

import psycopg2

from reimagina.orchestration.dbos_app import _executor_id, dbos_dsn

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0


def heartbeat() -> None:
    """Upsert this executor's liveness marker on a short direct connection."""
    with contextlib.closing(psycopg2.connect(dbos_dsn())) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs_runtime.ops_executor_heartbeat
              (executor_id, role, app_version, started_at, heartbeat_at)
            VALUES (%s, %s, %s, clock_timestamp(), clock_timestamp())
            ON CONFLICT (executor_id) DO UPDATE
            SET role = EXCLUDED.role,
                app_version = EXCLUDED.app_version,
                heartbeat_at = clock_timestamp()
            """,
            (
                _executor_id(),
                os.environ.get("REIMAGINA_EXECUTOR_ROLE", "app"),
                os.environ.get("REIMAGINA_APP_VERSION"),
            ),
        )
        conn.commit()


def interval_seconds() -> float:
    """The configured beat interval. Must be positive."""
    interval = float(
        os.environ.get("REIMAGINA_EXECUTOR_HEARTBEAT_S", DEFAULT_INTERVAL_S)
    )
    if interval <= 0:
        raise RuntimeError("REIMAGINA_EXECUTOR_HEARTBEAT_S must be positive")
    return interval


def start_heartbeat(
    stop: threading.Event, *, interval: float | None = None
) -> threading.Thread:
    """Beat until ``stop`` is set. Returns the daemon thread doing it.

    The first beat runs **before** the thread starts and is deliberately
    unguarded: a process that cannot reach its own database at startup should
    fail loudly rather than come up looking healthy.

    ⚠ Inside the loop the ``except`` is the point, and it is not defensive
    padding. ``heartbeat()`` opens a short direct connection, so any ordinary
    blip — a failover, a restart, a full connection pool, a network hiccup —
    raises here. Unhandled, that ends the *thread*, silently: the process keeps
    dequeuing and executing units while its row stops advancing forever, and
    nothing logs a word.

    Which is worse than a dead thread, because of what reads that row.
    ``orchestration.reclaim`` presumes an executor with a stale beat is dead
    and re-enqueues its pending workflows — so "stale but very much alive" is
    the one input that gate must never be handed. A missed beat is transient; a
    dead reporter is not.
    """
    beat_every = interval_seconds() if interval is None else interval
    heartbeat()

    def _loop() -> None:
        while not stop.wait(beat_every):
            try:
                heartbeat()
            except Exception:  # noqa: BLE001 — a missed beat must not end the loop
                logger.warning(
                    "executor heartbeat failed; retrying in %ss",
                    beat_every,
                    exc_info=True,
                )

    thread = threading.Thread(target=_loop, name="dbos-executor-heartbeat", daemon=True)
    thread.start()
    return thread
