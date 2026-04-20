"""Request-scoped dependencies for reimagina FastAPI routers.

A process-wide ThreadedConnectionPool is created on app startup (see
app.lifespan) and torn down on shutdown. Each request checks a connection
out, runs inside a transaction, and rolls back on teardown — so handlers
never see data leaked from other requests and nothing ever commits by
accident. Routers get the backend via get_backend(conn), which keeps
every route architecturally inside reimagina.storage (no raw SQL in the
API layer).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from threading import Lock

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from reimagina.storage import backend_for_connection
from reimagina.storage.interfaces import StorageBackend
from reimagina.tiles.defaults import tile_live_base_url, tile_snapshot_base_url


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
    "tile_snapshot_base_url",
]


_pool: ThreadedConnectionPool | None = None
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
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        _pool = ThreadedConnectionPool(
            _pool_min() if minconn is None else minconn,
            _pool_max() if maxconn is None else maxconn,
            api_dsn() if dsn is None else dsn,
        )


def shutdown_pool() -> None:
    """Close every connection in the pool. Safe to call when no pool exists."""
    global _pool
    with _pool_lock:
        if _pool is None:
            return
        _pool.closeall()
        _pool = None


def get_db_conn() -> Iterator[psycopg2.extensions.connection]:
    """Yield a pool-backed Postgres connection; rolls back + returns on teardown.

    Returns the connection to the pool even when the handler raises. If
    the rollback itself fails (connection is broken), the connection is
    closed rather than returned so the pool self-heals.
    """
    if _pool is None:
        raise PoolNotInitialised(
            "API connection pool is not initialised; "
            "apps.api.app.create_app() must run before any request arrives."
        )
    connection = _pool.getconn()
    broken = False
    try:
        connection.autocommit = False
        yield connection
    except Exception:
        try:
            connection.rollback()
        except psycopg2.Error:
            broken = True
        raise
    else:
        try:
            connection.rollback()
        except psycopg2.Error:
            broken = True
    finally:
        _pool.putconn(connection, close=broken)


def get_backend(
    conn: psycopg2.extensions.connection,
) -> StorageBackend:
    """Resolve the storage backend for the request's connection."""
    return backend_for_connection(conn)
