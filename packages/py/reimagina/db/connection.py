"""Database connection management via the storage manager."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from dotenv import load_dotenv

from reimagina.storage import connect as storage_connect
from reimagina.storage import default_dsn

load_dotenv()


def get_dsn(dsn: str | None = None) -> str:
    return dsn or default_dsn()


def connect(dsn: str | None = None):
    """Return a backend-specific connection. Caller owns the lifecycle."""
    return storage_connect(get_dsn(dsn))


@contextmanager
def managed_connection(dsn: str | None = None) -> Iterator[object]:
    """Yield a transaction-scoped connection and always close its socket.

    Psycopg2's connection context manager commits or rolls back but deliberately
    leaves the connection open. CLI commands and one-shot services need both
    semantics, so this is the canonical owner for short-lived connections.
    """
    conn = connect(dsn)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
