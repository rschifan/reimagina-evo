"""Swappable storage backends for local DuckDB and production PostGIS."""

from .manager import (
    DEFAULT_DEV_DSN,
    DEFAULT_POSTGRES_ADMIN_DSN,
    DEFAULT_TEST_DSN,
    backend_for_connection,
    backend_for_dsn,
    backend_name_for_dsn,
    connect,
    default_dsn,
    duckdb_path_from_dsn,
)

__all__ = [
    "DEFAULT_DEV_DSN",
    "DEFAULT_POSTGRES_ADMIN_DSN",
    "DEFAULT_TEST_DSN",
    "backend_for_connection",
    "backend_for_dsn",
    "backend_name_for_dsn",
    "connect",
    "default_dsn",
    "duckdb_path_from_dsn",
]
