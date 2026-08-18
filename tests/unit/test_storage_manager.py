from __future__ import annotations

import pytest

from reimagina.storage.manager import backend_name_for_dsn


def test_backend_name_for_dsn_supports_postgres():
    assert backend_name_for_dsn("postgresql://localhost/reimagina") == "postgres"
    assert backend_name_for_dsn("postgres://localhost/reimagina") == "postgres"


def test_backend_name_for_dsn_accepts_libpq_keyword_form():
    """The spelling `psycopg2` produces once a DSN carries a password.

    `connection.dsn` is returned verbatim while there is no password and
    re-spelled in keyword/value form when there is one, so this is exactly what
    a re-connect from a live connection sees in any deployment with credentials
    — and nothing in local development, where dev databases have none.
    """
    assert (
        backend_name_for_dsn("user=u password=xxx dbname=reimagina host=db port=5432")
        == "postgres"
    )
    assert backend_name_for_dsn("dbname=reimagina") == "postgres"


def test_backend_name_for_dsn_rejects_unsupported_scheme():
    with pytest.raises(ValueError):
        backend_name_for_dsn("sqlite:///data/local/reimagina.db")
    with pytest.raises(ValueError):
        backend_name_for_dsn("duckdb:///data/local/reimagina.db")


def test_backend_name_for_dsn_rejects_garbage():
    with pytest.raises(ValueError):
        backend_name_for_dsn("not a dsn at all")


def test_an_empty_database_url_is_not_a_usable_default(monkeypatch):
    """`parse_dsn("")` answers `{}` instead of raising.

    Reached only through the default, since an empty argument is falsy and falls
    back to `default_dsn()`. Without the emptiness check a `DATABASE_URL` that is
    set but blank would read as "connect to whatever libpq's defaults resolve
    to" — most likely a database named after the current user.
    """
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ValueError):
        backend_name_for_dsn()
