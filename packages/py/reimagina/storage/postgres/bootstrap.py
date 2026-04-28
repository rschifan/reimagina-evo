"""Bootstrap helpers for PostgreSQL/PostGIS environments."""

from __future__ import annotations

from urllib.parse import urlparse

import psycopg2
from psycopg2 import sql

from .migrations import run_postgres_migrations


def database_name_from_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    name = parsed.path.lstrip("/")
    if not name:
        raise ValueError(f"DSN has no database name: {dsn!r}")
    return name


def ensure_postgres_database(admin_dsn: str, target_dsn: str) -> bool:
    db_name = database_name_from_dsn(target_dsn)
    conn = psycopg2.connect(admin_dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone():
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        return True
    finally:
        conn.close()


def bootstrap_postgres(
    *,
    app_dsn: str,
    test_dsn: str,
    admin_dsn: str,
) -> dict[str, dict[str, str | bool]]:
    app_created = ensure_postgres_database(admin_dsn, app_dsn)
    test_created = ensure_postgres_database(admin_dsn, test_dsn)
    run_postgres_migrations(app_dsn)
    run_postgres_migrations(test_dsn)
    return {
        "app": {"dsn": app_dsn, "created": app_created, "migrated": True},
        "test": {"dsn": test_dsn, "created": test_created, "migrated": True},
    }
