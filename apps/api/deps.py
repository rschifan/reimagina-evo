"""Request-scoped dependencies for the FastAPI routers.

A process-wide ThreadedConnectionPool is created on app startup (see
app.lifespan) and torn down on shutdown. Each request checks a connection
out — waiting for one when all are busy — runs inside a transaction, and
rolls back on teardown — so handlers
never see data leaked from other requests and nothing ever commits by
accident. Routers get the backend via get_backend(conn), which keeps
every route architecturally inside reimagina.storage (no raw SQL in the
API layer).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from threading import Lock
from typing import Any

import psycopg2
from anyio import CancelScope
from fastapi import HTTPException
from psycopg2.pool import ThreadedConnectionPool
from starlette.concurrency import run_in_threadpool

from reimagina.storage import backend_for_connection
from reimagina.storage.interfaces import StorageBackend
from reimagina.tiles.defaults import (
    tile_live_base_url,
    tile_raster_base_url,
    tile_snapshot_base_url,
)


def api_dsn() -> str:
    """Resolve the API DSN at call time.

    Reading from the environment on every call means test fixtures can
    point the pool at a different database via ``monkeypatch.setenv``
    (the documented knob) and get consistent behaviour across the pool
    factory, readiness probe, and any future callers — instead of
    racing a module constant that was captured at import time.
    """
    return os.environ.get(
        "REIMAGINA_APP_DSN",
        "postgresql://localhost/reimagina_app",
    )


def _pool_min() -> int:
    return int(os.environ.get("REIMAGINA_API_POOL_MIN", "1"))


def _pool_max() -> int:
    return int(os.environ.get("REIMAGINA_API_POOL_MAX", "16"))


def _pool_wait_s() -> float:
    """How long a request waits for a free connection before a 503."""
    return float(os.environ.get("REIMAGINA_API_POOL_WAIT_S", "30"))


# Re-exported so importers of apps.api.deps keep working after helpers
# moved into reimagina.tiles.defaults.
__all__ = [
    "PoolNotInitialised",
    "api_dsn",
    "get_backend",
    "get_db_conn",
    "initialise_pool",
    "shutdown_pool",
    "tile_live_base_url",
    "tile_raster_base_url",
    "tile_snapshot_base_url",
]


_pool: ThreadedConnectionPool | None = None
#: One permit per pooled connection. ⚠ psycopg2's pool never blocks: past
#: ``maxconn`` it raises ``PoolError``, so a burst wider than the pool failed
#: instead of queueing. A map loading at a new zoom is exactly that burst —
#: measured, 40 concurrent tile requests came back 16 x 200 and 24 x 500 on an
#: idle server. Taking a permit before ``getconn`` makes the excess wait.
_slots: asyncio.Semaphore | None = None
_pool_lock = Lock()


class PoolNotInitialised(RuntimeError):
    """Raised if a request arrives before the lifespan hook set up the pool."""


def initialise_pool(
    *,
    dsn: str | None = None,
    minconn: int | None = None,
    maxconn: int | None = None,
) -> None:
    """Create the process-wide connection pool. Idempotent.

    All defaults resolve at call time via ``api_dsn()`` / env vars so
    tests and deployments can steer the pool through environment
    overrides without reloading this module.
    """
    global _pool, _slots
    with _pool_lock:
        if _pool is not None:
            return
        size = _pool_max() if maxconn is None else maxconn
        _pool = ThreadedConnectionPool(
            _pool_min() if minconn is None else minconn,
            size,
            api_dsn() if dsn is None else dsn,
        )
        _slots = asyncio.Semaphore(size)


def shutdown_pool() -> None:
    """Close every connection in the pool. Safe to call when no pool exists."""
    global _pool, _slots
    with _pool_lock:
        if _pool is None:
            return
        _pool.closeall()
        _pool = None
        _slots = None


async def get_db_conn() -> AsyncIterator[psycopg2.extensions.connection]:
    """Yield a pool-backed Postgres connection; rolls back + returns on teardown.

    Returns the connection to the pool even when the handler raises. If
    the rollback itself fails (connection is broken), the connection is
    closed rather than returned so the pool self-heals.

    When every connection is out, waits up to ``REIMAGINA_API_POOL_WAIT_S``
    for one, then answers 503 with ``Retry-After`` rather than hanging.
    """
    pool, slots = _pool, _slots
    if pool is None or slots is None:
        raise PoolNotInitialised(
            "API connection pool is not initialised; "
            "apps.api.app.create_app() must run before any request arrives."
        )
    # Wait on the event loop, before using a request thread. A blocking
    # semaphore here exhausted AnyIO's threads during map tile bursts: every
    # thread waited for a connection while the handlers holding connections
    # were queued behind them and could not run to release one.
    try:
        await asyncio.wait_for(slots.acquire(), timeout=_pool_wait_s())
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Every database connection is busy; try again shortly.",
            headers={"Retry-After": "1"},
        ) from None
    connection = None
    try:
        # Checkout can open a socket. Cancellation must not abandon a thread
        # that subsequently obtains a connection nobody knows to return.
        with CancelScope(shield=True):
            checkout = asyncio.create_task(run_in_threadpool(pool.getconn))
            try:
                connection = await asyncio.shield(checkout)
            except asyncio.CancelledError:
                connection = await _finish_database_task(checkout)
                raise
        assert connection is not None
        connection.autocommit = False
        yield connection
    finally:
        try:
            if connection is not None:
                with CancelScope(shield=True):
                    cleanup = asyncio.create_task(
                        run_in_threadpool(_return_connection, pool, connection)
                    )
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await _finish_database_task(cleanup)
                        raise
        finally:
            slots.release()


async def _finish_database_task(task: asyncio.Task[Any]) -> Any:
    # AnyIO shields parent cancellation scopes, but not Task.cancel(). Keep
    # the thread's result reachable and wait even if the request is cancelled
    # repeatedly; its connection permit belongs to the operation until it ends.
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


def _return_connection(pool, connection) -> None:
    broken = False
    try:
        connection.rollback()
    except psycopg2.Error:
        broken = True
    finally:
        pool.putconn(connection, close=broken)


def get_backend(
    conn: psycopg2.extensions.connection,
) -> StorageBackend:
    """Resolve the storage backend for the request's connection."""
    return backend_for_connection(conn)
