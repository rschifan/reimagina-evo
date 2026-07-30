"""Guards against planning work in one database and executing it in another.

The API derives a plan on its own connection; the worker executes it against
``DATABASE_URL_DIRECT``. If those two are not the same server *and* the same
database, the API produces a plan that is valid for rows the worker will never
see — a silent, total mismatch that no downstream check catches.
"""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache

import psycopg2

_IDENTITY_SQL = "SELECT current_database(), inet_server_addr(), current_setting('port')"
"""What identifies a database, as far as an unprivileged role can tell.

⚠ **Not `current_database()` alone**, which is what this module compared first.
Two different servers each hosting a database named ``reimagina`` — the ordinary
staging/production layout — compared equal, so the guard passed on precisely the
split it exists to detect.

⚠ **Not `pg_control_system().system_identifier`**, the exact answer. It is
superuser-restricted by default, so on a managed Postgres the application role
gets permission-denied and a safety check becomes an outage on every submit.

⚠ **`current_setting('port')`, not `inet_server_port()`.** Over a Unix socket
`inet_server_port()` is NULL while `current_setting('port')` still reports the
port — which is what distinguishes two local clusters, since two postmasters
cannot share one port on a host.

Residual limitation, stated rather than implied: with both sides on Unix sockets
`inet_server_addr()` is NULL on each, so identity rests on database name plus
port. That is sound for any deployment where sockets mean localhost, but it is
an assumption about deployment shape, not proof of identity.
"""


@lru_cache(maxsize=8)
def _direct_identity(direct_dsn: str) -> tuple[str, str | None, str, bool]:
    """Probe the worker-side database. Cached: it cannot change under us.

    Only this side is cached. The caller's connection varies per request and is
    read every time — caching it would defeat the guard the first time a process
    was pointed somewhere new.
    """
    with closing(psycopg2.connect(direct_dsn)) as direct, direct.cursor() as cur:
        cur.execute(f"{_IDENTITY_SQL}, to_regclass('jobs_runtime.job_run') IS NOT NULL")
        database, address, port, migrated = cur.fetchone()
    return database, address and str(address), port, migrated


def assert_same_domain_database(conn: object, direct_dsn: str) -> None:
    """Verify the caller connection and the worker DSN name one database.

    Roles and connection routes may differ; database identity may not.
    """
    with conn.cursor() as cur:
        cur.execute(_IDENTITY_SQL)
        caller_database, caller_address, caller_port = cur.fetchone()
    caller_address = caller_address and str(caller_address)

    direct_database, direct_address, direct_port, migrated = _direct_identity(
        direct_dsn
    )

    if not migrated:
        raise RuntimeError(
            "DATABASE_URL_DIRECT does not point at a migrated reimagina database "
            "(jobs_runtime.job_run is missing)"
        )
    # Fail closed on asymmetry. One side reporting a server address while the
    # other does not means one is a socket and the other is TCP — they may well
    # be the same server, but nothing here can show that, and a guard that
    # guesses is worse than one that refuses.
    if (caller_address is None) != (direct_address is None):
        raise RuntimeError(
            "submit connection and DATABASE_URL_DIRECT reach Postgres differently "
            f"(socket vs TCP: {caller_address!r} vs {direct_address!r}); make both "
            "DSNs the same shape so their identity can be compared"
        )
    if (caller_database, caller_address, caller_port) != (
        direct_database,
        direct_address,
        direct_port,
    ):
        raise RuntimeError(
            "submit connection and DATABASE_URL_DIRECT name different databases "
            f"({caller_database!r}@{caller_address}:{caller_port} != "
            f"{direct_database!r}@{direct_address}:{direct_port})"
        )
