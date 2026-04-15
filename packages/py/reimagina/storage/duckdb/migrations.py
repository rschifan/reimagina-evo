"""DuckDB migration runner with revision tracking."""
from __future__ import annotations

from pathlib import Path

from infra.db.migrations.common.manifest import REVISIONS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def duckdb_revision_dir() -> Path:
    return _repo_root() / "infra" / "db" / "migrations" / "duckdb"


def run_duckdb_migrations(connection) -> None:
    """Apply all pending DuckDB SQL migrations in revision order.

    Applied revisions are recorded in ``_duckdb_migrations`` so that
    re-running this function on an already-migrated database is a no-op.
    All SQL files use ``IF NOT EXISTS`` / ``ON CONFLICT DO NOTHING``, so
    existing databases created before revision tracking was added will be
    safely re-migrated on the first call and then tracked going forward.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS _duckdb_migrations (
            revision   VARCHAR     PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    base = duckdb_revision_dir()
    for revision in REVISIONS:
        already_applied = connection.execute(
            "SELECT revision FROM _duckdb_migrations WHERE revision = ?",
            [revision],
        ).fetchone()
        if already_applied is not None:
            continue

        sql_path = next(base.glob(f"{revision}_*.sql"), None)
        if sql_path is None:
            raise FileNotFoundError(f"Missing DuckDB migration for revision {revision!r}")
        connection.execute(sql_path.read_text())
        connection.execute(
            "INSERT INTO _duckdb_migrations (revision) VALUES (?)",
            [revision],
        )
