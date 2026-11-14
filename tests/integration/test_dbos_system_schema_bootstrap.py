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

from contextlib import closing
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

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


def _with_database(dsn: str, name: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{name}"))


def _run(dsn: str, *statements: str) -> None:
    """Execute DDL on ``dsn``, autocommitting, and close the socket after.

    Autocommit because ``CREATE``/``DROP DATABASE`` cannot run inside a
    transaction. ``closing`` because psycopg2's own context manager scopes the
    *transaction* and leaves the socket open — the distinction this repo spells
    out wherever it opens a short-lived connection.
    """
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)


@pytest.fixture
def unprovisioned_db(migrated_db: str):
    """A database of this test's own, holding only what the guard inspects.

    ⚠ Not ``migrated_db`` with the schema dropped first, which is how this
    started. That fixture is session-scoped, so dropping ``dbos`` out of it
    reaches every other test in the run: today nothing sorting earlier has
    workflow state to lose, but that is an accident of module names rather
    than a property anyone maintains, and a randomised order or one new
    enqueueing test turns it into another test failing for a reason several
    files away. Worse, the restore *was* the function under test — so a lock
    timeout or a dropped connection left the schema missing for everything
    that followed, converting one failure into a cascade.

    A fresh database cannot have been provisioned by anything, which is the
    non-vacuity the drop was reaching for, and nothing else can observe it.
    ``_assert_domain_database`` looks at exactly one relation, so creating
    that one is enough to get past the guard without running Alembic.
    """
    admin = _with_database(migrated_db, "postgres")
    name = f"reimagina_dbosprov_{uuid4().hex[:12]}"
    dsn = _with_database(migrated_db, name)

    # Three connections, and only three: the database cannot be created from
    # inside itself, and the schema cannot be created from outside it.
    _run(admin, f'CREATE DATABASE "{name}"')
    try:
        _run(
            dsn,
            "CREATE SCHEMA jobs_runtime",
            "CREATE TABLE jobs_runtime.job_run (id uuid PRIMARY KEY)",
        )
        yield dsn
    finally:
        # FORCE because a failed assertion can leave DBOS's pool open.
        _run(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_ensure_system_database_creates_the_schema_a_client_cannot(
    unprovisioned_db: str,
):
    """Against a database nothing has provisioned, so it cannot pass vacuously."""
    assert _regclass(unprovisioned_db, "dbos.workflow_status") is None, (
        "precondition: the schema must be absent for this test to mean anything"
    )

    ensure_system_database(unprovisioned_db)

    assert _regclass(unprovisioned_db, "dbos.workflow_status") is not None


def test_ensure_system_database_is_idempotent(unprovisioned_db: str):
    """Bootstrap is re-run on every deploy, and `make migrate` is routine.

    From nothing, so the second call is the one under test: the first does the
    real migration and the second must find the recorded version current and
    return rather than repeat it.
    """
    ensure_system_database(unprovisioned_db)
    ensure_system_database(unprovisioned_db)
    assert _regclass(unprovisioned_db, "dbos.workflow_status") is not None


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
    not_ours = _with_database(migrated_db, "postgres")
    with pytest.raises(RuntimeError, match="not a migrated reimagina database"):
        ensure_system_database(not_ours)

    assert _regclass(not_ours, "dbos.workflow_status") is None, (
        "the guard must run before DBOS touches the database at all"
    )
