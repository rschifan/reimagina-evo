"""Postgres-native executor liveness, deliberately separate from recovery."""

from __future__ import annotations

import contextlib
import os

import psycopg2

from reimagina.orchestration.dbos_app import _executor_id, dbos_dsn


def heartbeat() -> None:
    """Upsert this executor's liveness marker on a short direct connection."""
    with contextlib.closing(psycopg2.connect(dbos_dsn())) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs_runtime.ops_executor_heartbeat
              (executor_id, role, app_version, started_at, heartbeat_at)
            VALUES (%s, %s, %s, clock_timestamp(), clock_timestamp())
            ON CONFLICT (executor_id) DO UPDATE
            SET role = EXCLUDED.role,
                app_version = EXCLUDED.app_version,
                heartbeat_at = clock_timestamp()
            """,
            (
                _executor_id(),
                os.environ.get("REIMAGINA_EXECUTOR_ROLE", "app"),
                os.environ.get("REIMAGINA_APP_VERSION"),
            ),
        )
        conn.commit()
