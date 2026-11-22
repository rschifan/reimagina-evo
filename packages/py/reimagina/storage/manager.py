"""Backend selection and connection routing for storage."""

from __future__ import annotations

import os
import re

import psycopg2
import psycopg2.extensions

from .postgres.backend import PostgresStorageBackend
from .interfaces import StorageBackend


DEFAULT_DEV_DSN = "postgresql://localhost/reimagina"
DEFAULT_TEST_DSN = "postgresql://localhost/reimagina_test"
DEFAULT_POSTGRES_ADMIN_DSN = "postgresql://localhost/postgres"


def default_dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DEV_DSN)


def backend_name_for_dsn(dsn: str | None = None) -> str:
    """Validate that a DSN names a Postgres server, in *either* libpq spelling.

    ⚠ A URL-prefix test is not sufficient, and the way it fails is invisible.
    ``psycopg2`` hands back ``connection.dsn`` verbatim only while the DSN
    carries no password; add one and it re-spells the whole thing in
    keyword/value form so the secret can be obscured::

        postgresql://u@host/db            -> postgresql://u@host/db
        postgresql://u:secret@host/db     -> user=u password=xxx dbname=db host=host

    So code that re-connects using a live connection's own ``conn.dsn`` — which
    is how a failed unit re-records its ``job_run`` after a rollback — worked in
    local development, where dev databases have no password, and failed on every
    deployment that has one. `record_failed_job_run` catches broadly because
    provenance is best-effort, so the loss surfaced as a log line rather than an
    error: failures silently stopped leaving a row.

    `parse_dsn` understands both spellings and still rejects a non-Postgres
    scheme (``duckdb://``, ``sqlite://``) and anything unparseable, which is the
    only thing the prefix test was really there to do.
    """
    resolved = dsn or default_dsn()
    try:
        parsed = psycopg2.extensions.parse_dsn(resolved)
    except psycopg2.ProgrammingError as exc:
        raise ValueError(_unsupported(resolved)) from exc
    # An empty string parses to {} rather than raising, and would otherwise be
    # accepted as "connect to whatever libpq's defaults resolve to".
    if not parsed:
        raise ValueError(_unsupported(resolved))
    return "postgres"


def _unsupported(dsn: str) -> str:
    """The refusal message, naming at most the scheme: the DSN is unparseable,
    so `mask_dsn` cannot find its password, and the CLI prints this line."""
    scheme, sep, _ = dsn.partition("://")
    shown = (
        f"scheme {scheme!r}"
        if sep and re.fullmatch(r"[A-Za-z][\w+.-]*", scheme)
        else "not a URL or key=value DSN"
    )
    return f"Unsupported storage DSN ({shown}); expected postgresql://… or key=value"


def connect(dsn: str | None = None):
    resolved = dsn or default_dsn()
    # Validate the scheme (raises on anything non-postgres) before connecting.
    backend_name_for_dsn(resolved)
    return psycopg2.connect(resolved)


def backend_for_connection(connection) -> StorageBackend:
    if isinstance(connection, psycopg2.extensions.connection):
        return PostgresStorageBackend(connection)
    raise TypeError(f"Unsupported connection type: {type(connection)!r}")


def mask_dsn(dsn: str | None) -> str:
    """Return ``dsn`` with any password replaced by ``***`` for safe logging or
    echo. DSNs without a password are returned unchanged.

    libpq takes the password from a URL's userinfo, from a ``?password=``
    query parameter, or from a ``password=`` keyword, and this app accepts all
    three (see `backend_name_for_dsn`). Reading only ``urlparse().password``
    returned the last two verbatim, so `parse_dsn` does the reading and the
    masked DSN comes back in keyword/value form, as psycopg2 itself spells it.
    """
    if not dsn:
        return ""
    try:
        parsed = psycopg2.extensions.parse_dsn(dsn)
    except psycopg2.ProgrammingError:
        return "(dsn parse failed)"
    if "password" not in parsed:
        return dsn
    return psycopg2.extensions.make_dsn(**{**parsed, "password": "***"})
