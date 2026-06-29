"""Backend selection and connection routing for swappable storage."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import psycopg2
import psycopg2.extensions

from .duckdb.backend import DuckDBStorageBackend
from .postgres.backend import PostgresStorageBackend
from .interfaces import StorageBackend


DEFAULT_DEV_DSN = "duckdb:///data/local/reimagina.duckdb"
DEFAULT_TEST_DSN = "duckdb:///data/local/reimagina_test.duckdb"
DEFAULT_POSTGRES_ADMIN_DSN = "postgresql://localhost/postgres"


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DEV_DSN)


def backend_name_for_dsn(dsn: str | None = None) -> str:
    resolved = dsn or default_dsn()
    if resolved.startswith("duckdb://"):
        return "duckdb"
    if resolved.startswith("postgresql://") or resolved.startswith("postgres://"):
        return "postgres"
    raise ValueError(f"Unsupported storage DSN: {resolved!r}")


def duckdb_path_from_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.scheme != "duckdb":
        raise ValueError(f"Expected duckdb DSN, got {dsn!r}")
    path = parsed.path or ""
    if path == "/:memory:":
        return ":memory:"
    if dsn.startswith("duckdb:////"):
        return "/" + path.lstrip("/")
    if not path:
        raise ValueError(f"DuckDB DSN must include a database path: {dsn!r}")
    return path.lstrip("/")


def connect(dsn: str | None = None):
    resolved = dsn or default_dsn()
    backend_name = backend_name_for_dsn(resolved)
    if backend_name == "duckdb":
        path = duckdb_path_from_dsn(resolved)
        conn = duckdb.connect(path)
        conn.execute("LOAD spatial")
        return conn
    return psycopg2.connect(resolved)


def backend_for_connection(connection) -> StorageBackend:
    if isinstance(connection, duckdb.DuckDBPyConnection):
        return DuckDBStorageBackend(connection)
    if isinstance(connection, psycopg2.extensions.connection):
        return PostgresStorageBackend(connection)
    raise TypeError(f"Unsupported connection type: {type(connection)!r}")


def mask_dsn(dsn: str | None) -> str:
    """Return ``dsn`` with any password replaced by ``***`` for safe logging or
    echo. DSNs without a password (e.g. ``duckdb:///…``) are returned unchanged.
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


def ensure_duckdb_parent(path: str) -> None:
    if path == ":memory:":
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
