"""Backend-aware bootstrap helpers for DuckDB dev and Postgres production."""
from __future__ import annotations

from pathlib import Path

from reimagina.storage import (
    DEFAULT_DEV_DSN,
    DEFAULT_POSTGRES_ADMIN_DSN,
    DEFAULT_TEST_DSN as DEFAULT_STORAGE_TEST_DSN,
    backend_for_connection,
    backend_name_for_dsn,
    duckdb_path_from_dsn,
)
from reimagina.storage.duckdb.bootstrap import bootstrap_duckdb
from reimagina.storage.duckdb.migrations import run_duckdb_migrations
from reimagina.storage.postgres.bootstrap import (
    bootstrap_postgres,
    ensure_postgres_database,
)
from reimagina.storage.postgres.migrations import run_postgres_migrations


DEFAULT_APP_DSN = DEFAULT_DEV_DSN
DEFAULT_TEST_DSN = DEFAULT_STORAGE_TEST_DSN
DEFAULT_ADMIN_DSN = DEFAULT_POSTGRES_ADMIN_DSN

REQUIRED_RELATIONS = [
    "curated_geo.data_source_catalog",
    "curated_geo.ingestion_run",
    "curated_geo.feature_type_catalog",
    "curated_geo.feature",
    "serving_geo.feature_v",
]


def ensure_database(admin_dsn: str, target_dsn: str) -> bool:
    backend_name = backend_name_for_dsn(target_dsn)
    if backend_name == "postgres":
        return ensure_postgres_database(admin_dsn, target_dsn)
    path = duckdb_path_from_dsn(target_dsn)
    if path == ":memory:":
        return True
    db_path = Path(path)
    existed = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return not existed


def run_migrations(dsn: str, connection=None) -> None:
    backend_name = backend_name_for_dsn(dsn)
    if backend_name == "postgres":
        run_postgres_migrations(dsn)
        return
    if connection is None:
        raise ValueError("DuckDB migrations require an open connection")
    run_duckdb_migrations(connection)


def bootstrap_local_platform(
    *,
    app_dsn: str = DEFAULT_APP_DSN,
    test_dsn: str = DEFAULT_TEST_DSN,
    admin_dsn: str = DEFAULT_ADMIN_DSN,
) -> dict[str, dict[str, str | bool]]:
    app_backend = backend_name_for_dsn(app_dsn)
    test_backend = backend_name_for_dsn(test_dsn)
    if app_backend == "duckdb" and test_backend == "duckdb":
        return bootstrap_duckdb(
            app_path=duckdb_path_from_dsn(app_dsn),
            test_path=duckdb_path_from_dsn(test_dsn),
        )
    if app_backend == "postgres" and test_backend == "postgres":
        return bootstrap_postgres(app_dsn=app_dsn, test_dsn=test_dsn, admin_dsn=admin_dsn)
    raise ValueError("Mixed backend bootstrap is not supported in one call")


def verify_required_relations(conn, relations: list[str] | None = None) -> dict[str, bool]:
    required = relations or REQUIRED_RELATIONS
    return backend_for_connection(conn).verify_required_relations(required)
