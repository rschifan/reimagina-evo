"""Request-scoped dependencies for reimagina FastAPI routers.

A per-request psycopg2 connection opens against `REIMAGINA_APP_DSN` (or the
Postgres default), is wrapped in a transaction that rolls back on response,
and is closed in the dependency's finally block. Any handler that needs
domain access goes through `get_backend()` — no router ever touches a raw
cursor, so every route stays architecturally inside `reimagina.storage`.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg2

from reimagina.storage import backend_for_connection
from reimagina.storage.interfaces import StorageBackend


DEFAULT_API_DSN = os.environ.get(
    "REIMAGINA_APP_DSN",
    "postgresql://localhost/reimagina_app",
)


def get_db_conn() -> Iterator[psycopg2.extensions.connection]:
    """Yield a request-scoped Postgres connection; rolls back after response."""
    connection = psycopg2.connect(DEFAULT_API_DSN)
    connection.autocommit = False
    try:
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def get_backend(
    conn: psycopg2.extensions.connection,
) -> StorageBackend:
    """Resolve the storage backend for the request's connection.

    Kept as a thin function (not a FastAPI dependency) so routers
    compose it from the connection dep explicitly: `backend = get_backend(conn)`.
    This reads naturally at the call site and keeps the connection lifecycle
    the one source of truth.
    """
    return backend_for_connection(conn)
