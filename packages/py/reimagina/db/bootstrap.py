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
    direct_dsn: str | None = None,
) -> dict[str, dict[str, str | bool]]:
    backend_name_for_dsn(app_dsn)  # validate scheme
    backend_name_for_dsn(test_dsn)
    if direct_dsn is not None:
        backend_name_for_dsn(direct_dsn)
    # Both DDL steps below take `direct_dsn` when there is one — Alembic here,
    # the ops-plane schema at the end. One route for everything this function
    # migrates.
    result = bootstrap_postgres(
        app_dsn=app_dsn,
        test_dsn=test_dsn,
        admin_dsn=admin_dsn,
        direct_dsn=direct_dsn,
    )

    # Seed the indicator catalog so a freshly-bootstrapped DB has it without a
    # manual `indicators seed-catalog` step. The impact engine reads the Python
    # specs and the write-triggers read this table; seeding here keeps the two
    # from drifting on a fresh deploy. Idempotent (ON CONFLICT).
    #
    # The asset registry is seeded from code for the same reason, and from *here*
    # rather than from a migration body: r006/r015/r016/r019/r020 each insert
    # their own explore.layer_catalog rows and that drifted far enough to need
    # scripts/repair_layer_catalog.sql. Assets are seeded after feature types
    # exist, since a feature-backed asset resolves its feature_type_id.
    from reimagina.assets.registry import seed_assets
    from reimagina.db import connect
    from reimagina.indicators.catalog import seed_catalog

    with connect(app_dsn) as conn:
        seed_catalog(conn)
        seed_assets(conn)
        conn.commit()

    # The ops plane's own schema, for the same reason as the two seeds above: a
    # bootstrapped database should be one the system can actually be used
    # against. DBOS creates its schema inside `launch()`, which only a worker
    # calls — so without this the API, which submits as a `DBOSClient` and by
    # design never migrates, answered every `POST /jobs` with a 500 until a
    # worker had run at least once. The dashboard offered a Run button from the
    # moment the catalog above was seeded, so the gap was reachable long before
    # anything could serve it.
    #
    # Imported here rather than at module scope: `dbos_app` pulls the engine's
    # client library in at import time, and bootstrap must stay importable for
    # callers that never touch the ops plane.
    from reimagina.orchestration import dbos_app

    # `direct_dsn` when the caller named one: this is a schema migration, and
    # `ensure_system_database` explains at length why one must not cross a
    # transaction-mode pooler. Falling back to `app_dsn` keeps the common local
    # install working, where there is one DSN and it is already direct.
    #
    # ⚠ The parameter wins over the environment, and the resolution happens in
    # the CLI rather than here — `reimagina.db` reads no environment variables,
    # and `--direct-dsn` beating `$DATABASE_URL_DIRECT` is the precedence
    # `raster_zonal_stats` and the two CLI commands that already take this flag
    # all use. The reverse silently overrode the caller: the runbook tells an
    # operator to pass `--app-dsn "$DATABASE_URL_DIRECT"`, and an env var left
    # pointing at some other database — which the benchmark scripts document
    # exporting — would have sent the ops-plane schema there instead, with
    # `_assert_domain_database` none the wiser because that database is
    # migrated too.
    dbos_app.ensure_system_database(direct_dsn or app_dsn)
    return result


def verify_required_relations(
    conn, relations: list[str] | None = None
) -> dict[str, bool]:
    required = relations or REQUIRED_RELATIONS
    return backend_for_connection(conn).verify_required_relations(required)
