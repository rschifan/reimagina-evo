"""The ops plane's own schema must exist before the first submit, not after.

`DBOSClient` — what an API process in the default ``client`` execution mode
submits through — never runs DBOS's schema migrations. Its docstring is
explicit: "the system database must already have been created by a DBOS
application". Only ``DBOS.launch()`` creates it, and only a worker launches.

So on a database migrated by `make migrate` and never yet touched by a worker,
``POST /jobs`` failed with ``UndefinedTable: relation "dbos.workflow_status"
does not exist`` — a 500 on the first action a freshly-installed Ops dashboard
offers, with the Run button sitting right next to an "no executor has ever
reported" banner that correctly says a worker is needed but does not say the
submit itself cannot work. Bootstrap owns this now.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

from reimagina.orchestration.dbos_app import ensure_system_database


def _regclass(dsn: str, relation: str):
    with (
        psycopg2.connect(dsn) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT to_regclass(%s)", (relation,))
        return cur.fetchone()[0]


def _drop_dbos_schema(dsn: str) -> None:
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS dbos CASCADE")


def test_ensure_system_database_creates_the_schema_a_client_cannot(migrated_db: str):
    """Dropped first, and the absence asserted, so this cannot pass vacuously.

    Everything else in this suite that reaches the orchestrator either launches
    DBOS or provisions through bootstrap, so by the time this runs the schema
    is usually already there — and an assertion that it exists would then hold
    whether or not `ensure_system_database` did anything at all. Restoring it
    is the function under test, which is why the restore is the assertion and
    not a `finally`: if it does not work, this fails here rather than leaking a
    broken database into whatever runs next.
    """
    _drop_dbos_schema(migrated_db)
    assert (
        _regclass(migrated_db, "dbos.workflow_status") is None
    ), "precondition: the schema must be absent for this test to mean anything"

    ensure_system_database(migrated_db)

    assert _regclass(migrated_db, "dbos.workflow_status") is not None


def test_ensure_system_database_is_idempotent(migrated_db: str):
    """Bootstrap is re-run on every deploy, and `make migrate` is routine."""
    ensure_system_database(migrated_db)
    ensure_system_database(migrated_db)
    assert _regclass(migrated_db, "dbos.workflow_status") is not None


def test_ensure_system_database_refuses_a_database_that_is_not_ours(migrated_db: str):
    """DBOS *creates* a database rather than failing when one is absent, so a
    typo in the DSN would otherwise install a system schema somewhere nobody
    looks and report success. `_assert_domain_database` already guards
    `launch()`; the bootstrap path must not be the hole in that guard.

    Aimed at ``postgres`` on the same server, reached with the same
    credentials: reachable, so the failure under test is "this database is not
    reimagina's" and not "the host refused us" — which is what a DSN typo
    inside the right cluster actually looks like.
    """
    not_ours = urlunparse(urlparse(migrated_db)._replace(path="/postgres"))
    with pytest.raises(RuntimeError, match="not a migrated reimagina database"):
        ensure_system_database(not_ours)

    assert (
        _regclass(not_ours, "dbos.workflow_status") is None
    ), "the guard must run before DBOS touches the database at all"
