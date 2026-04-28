"""Bootstrap helpers for DuckDB dev/test environments."""

from __future__ import annotations

from pathlib import Path

import duckdb

from .migrations import run_duckdb_migrations


def ensure_duckdb_database(path: str) -> bool:
    if path == ":memory:":
        return True
    db_path = Path(path)
    existed = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return not existed


def bootstrap_duckdb(
    *, app_path: str, test_path: str
) -> dict[str, dict[str, str | bool]]:
    app_created = ensure_duckdb_database(app_path)
    test_created = ensure_duckdb_database(test_path)

    with duckdb.connect(app_path) as app_conn:
        app_conn.execute("INSTALL spatial")
        app_conn.execute("LOAD spatial")
        run_duckdb_migrations(app_conn)

    with duckdb.connect(test_path) as test_conn:
        test_conn.execute("INSTALL spatial")
        test_conn.execute("LOAD spatial")
        run_duckdb_migrations(test_conn)

    return {
        "app": {
            "dsn": f"duckdb:///{app_path}",
            "created": app_created,
            "migrated": True,
        },
        "test": {
            "dsn": f"duckdb:///{test_path}",
            "created": test_created,
            "migrated": True,
        },
    }
