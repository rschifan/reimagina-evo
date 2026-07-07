"""Backend selection and connection routing for storage."""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extensions

from .postgres.backend import PostgresStorageBackend
from .interfaces import StorageBackend


DEFAULT_DEV_DSN = "postgresql://localhost/reimagina"
DEFAULT_TEST_DSN = "postgresql://localhost/reimagina_test"
DEFAULT_POSTGRES_ADMIN_DSN = "postgresql://localhost/postgres"


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DEV_DSN)


def backend_name_for_dsn(dsn: str | None = None) -> str:
    resolved = dsn or default_dsn()
    if resolved.startswith("postgresql://") or resolved.startswith("postgres://"):
        return "postgres"
    raise ValueError(f"Unsupported storage DSN: {resolved!r}")


def connect(dsn: str | None = None):
    resolved = dsn or default_dsn()
    # Validate the scheme (raises on anything non-postgres) before connecting.
    backend_name_for_dsn(resolved)
    return psycopg2.connect(resolved)


def backend_for_connection(connection) -> StorageBackend:
    if isinstance(connection, psycopg2.extensions.connection):
        return PostgresStorageBackend(connection)
    raise TypeError(f"Unsupported connection type: {type(connection)!r}")


def mask_dsn(dsn: str | None) -> str:
    """Return ``dsn`` with any password replaced by ``***`` for safe logging or
    echo. DSNs without a password are returned unchanged.
    """
    if not dsn:
        return ""
    from urllib.parse import urlparse, urlunparse

    try:
        parts = urlparse(dsn)
        if not parts.password:
            return dsn
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{netloc}"
        return urlunparse(parts._replace(netloc=netloc))
    except Exception:
        return "(dsn parse failed)"
