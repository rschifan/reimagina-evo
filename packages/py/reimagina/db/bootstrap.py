"""Bootstrap helpers for the Postgres storage backend."""

from __future__ import annotations

from reimagina.storage import (
    DEFAULT_DEV_DSN,
    DEFAULT_POSTGRES_ADMIN_DSN,
    DEFAULT_TEST_DSN as DEFAULT_STORAGE_TEST_DSN,
    backend_for_connection,
    backend_name_for_dsn,
)
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
    backend_name_for_dsn(target_dsn)  # validate scheme
    return ensure_postgres_database(admin_dsn, target_dsn)


def run_migrations(dsn: str) -> None:
    backend_name_for_dsn(dsn)  # validate scheme
    run_postgres_migrations(dsn)


def bootstrap_local_platform(
    *,
    app_dsn: str = DEFAULT_APP_DSN,
    test_dsn: str = DEFAULT_TEST_DSN,
    admin_dsn: str = DEFAULT_ADMIN_DSN,
) -> dict[str, dict[str, str | bool]]:
    backend_name_for_dsn(app_dsn)  # validate scheme
    backend_name_for_dsn(test_dsn)
    result = bootstrap_postgres(app_dsn=app_dsn, test_dsn=test_dsn, admin_dsn=admin_dsn)

    # Seed the indicator catalog so a freshly-bootstrapped DB has it without a
    # manual `indicators seed-catalog` step. The impact engine reads the Python
    # specs and the write-triggers read this table; seeding here keeps the two
    # from drifting on a fresh deploy. Idempotent (ON CONFLICT).
    from reimagina.db import connect
    from reimagina.indicators.catalog import seed_catalog

    with connect(app_dsn) as conn:
        seed_catalog(conn)
        conn.commit()
    return result


def verify_required_relations(
    conn, relations: list[str] | None = None
) -> dict[str, bool]:
    required = relations or REQUIRED_RELATIONS
    return backend_for_connection(conn).verify_required_relations(required)
