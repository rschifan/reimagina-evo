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
    direct_dsn: str | None = None,
) -> dict[str, dict[str, str | bool]]:
    """Create both databases and migrate them to head.

    ``direct_dsn`` is an alternate *route* to the app database, not a different
    database: in the compose stack ``app_dsn`` reaches pgbouncer on 5432 and
    this reaches Postgres itself on 5433, same name either side. Given one,
    Alembic takes it, because ``.env.example`` names Alembic first among the
    things the direct DSN exists for, and because the ops-plane schema beside
    it already migrates direct — one function answering "which DSN carries
    DDL" two ways is how the next person gets it wrong.

    Alembic would in fact survive the pooler: every migration here avoids
    ``CONCURRENTLY`` precisely so it runs inside one transaction, and a
    transaction-mode pooler pins a backend for a transaction's duration. So
    this is consistency rather than a repair — unlike the DBOS system schema,
    whose *session* advisory lock the pooler really does break.

    The test database keeps its own DSN. No direct route is supplied for it,
    and deriving one by swapping a database name into ``direct_dsn`` would
    assume the two live on the same server — exactly the inference that puts
    DDL somewhere nobody expects.
    """
    if direct_dsn is not None:
        # Host and port must be free to differ — that is what pooled-vs-direct
        # *is*. The database name must not: a direct DSN naming something else
        # would migrate one database while `ensure_postgres_database` created,
        # and the ops plane provisioned, another. Compared as strings because
        # it is free and catches the realistic typo; `assert_same_domain_database`
        # does the server-side version later, when there is a run to protect.
        app_name = database_name_from_dsn(app_dsn)
        direct_name = database_name_from_dsn(direct_dsn)
        if app_name != direct_name:
            raise ValueError(
                f"the direct DSN names a different database than the app DSN "
                f"({direct_name!r} != {app_name!r}); a direct DSN is another "
                "route to the same database, so only host and port may differ"
            )

    app_created = ensure_postgres_database(admin_dsn, app_dsn)
    test_created = ensure_postgres_database(admin_dsn, test_dsn)
    run_postgres_migrations(direct_dsn or app_dsn)
    run_postgres_migrations(test_dsn)
    return {
        # The app's own DSN, not the route the migration travelled: this dict
        # is what `make migrate` prints, and it should name the DSN the
        # application will use.
        "app": {"dsn": app_dsn, "created": app_created, "migrated": True},
        "test": {"dsn": test_dsn, "created": test_created, "migrated": True},
    }
