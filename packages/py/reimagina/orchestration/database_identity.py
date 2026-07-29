"""Guards against planning work in one database and executing it in another."""

from __future__ import annotations

from contextlib import closing

import psycopg2


def assert_same_domain_database(conn: object, direct_dsn: str) -> None:
    """Verify the caller connection and direct worker connection name one database.

    Roles and connection routes may differ; database identity may not. A mismatch
    makes the API derive a valid plan that a worker cannot safely execute.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        caller_database = cur.fetchone()[0]
    with closing(psycopg2.connect(direct_dsn)) as direct:
        with direct.cursor() as cur:
            cur.execute(
                "SELECT current_database(), to_regclass('jobs_runtime.job_run')"
            )
            direct_database, job_run = cur.fetchone()
    if job_run is None:
        raise RuntimeError(
            "DATABASE_URL_DIRECT does not point at a migrated reimagina database "
            "(jobs_runtime.job_run is missing)"
        )
    if caller_database != direct_database:
        raise RuntimeError(
            "submit connection and DATABASE_URL_DIRECT name different databases "
            f"({caller_database!r} != {direct_database!r})"
        )
