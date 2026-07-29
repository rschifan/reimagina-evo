"""The API planner and the Hatchet worker must target the same domain database.

A mismatch is silent and total: the API derives a plan against rows the worker
will never see. These pin what "same" means, because the first version of this
guard compared `current_database()` alone and therefore passed on the exact
staging-vs-production split it was written to catch.
"""

from __future__ import annotations

import pytest

from reimagina.orchestration import database_identity
from reimagina.orchestration.database_identity import assert_same_domain_database


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_):
        pass

    def fetchone(self):
        return self._row


class _Connection:
    """Answers any query with one canned row — the shape is what matters here."""

    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    """The direct-side probe is memoised per DSN; tests must not share it."""
    database_identity._direct_identity.cache_clear()
    yield
    database_identity._direct_identity.cache_clear()


def _direct(monkeypatch, row):
    monkeypatch.setattr(
        "reimagina.orchestration.database_identity.psycopg2.connect",
        lambda _dsn: _Connection(row),
    )


# caller rows are (database, address, port); direct rows add the migrated flag.
TCP_A = ("reimagina", "10.0.0.1", "5432")
TCP_B = ("reimagina", "10.0.0.2", "5432")
SOCKET_5432 = ("reimagina", None, "5432")
SOCKET_5433 = ("reimagina", None, "5433")


def test_accepts_the_same_server_and_database(monkeypatch):
    _direct(monkeypatch, (*TCP_A, True))
    assert_same_domain_database(_Connection(TCP_A), "postgresql://direct")


def test_rejects_the_same_database_name_on_a_different_server(monkeypatch):
    """The finding this guard exists for.

    Both sides answer `current_database() == 'reimagina'`, so a name-only
    comparison passes and the API plans against staging while the worker
    executes against production.
    """
    _direct(monkeypatch, (*TCP_B, True))

    with pytest.raises(RuntimeError, match="different databases"):
        assert_same_domain_database(_Connection(TCP_A), "postgresql://direct")


def test_rejects_two_local_clusters_differing_only_by_port(monkeypatch):
    """Why `current_setting('port')` and not `inet_server_port()`.

    Over a Unix socket `inet_server_port()` is NULL on both sides, so only the
    port setting distinguishes two postmasters on one host.
    """
    _direct(monkeypatch, (*SOCKET_5433, True))

    with pytest.raises(RuntimeError, match="different databases"):
        assert_same_domain_database(_Connection(SOCKET_5432), "postgresql://direct")


def test_rejects_socket_versus_tcp_asymmetry(monkeypatch):
    """Fail closed: they may be the same server, but nothing here can show it."""
    _direct(monkeypatch, (*TCP_A, True))

    with pytest.raises(RuntimeError, match="socket vs TCP"):
        assert_same_domain_database(_Connection(SOCKET_5432), "postgresql://direct")


def test_rejects_an_unmigrated_direct_database(monkeypatch):
    _direct(monkeypatch, (*TCP_A, False))

    with pytest.raises(RuntimeError, match="migrated reimagina"):
        assert_same_domain_database(_Connection(TCP_A), "postgresql://direct")


def test_the_direct_probe_is_cached_and_the_caller_side_is_not(monkeypatch):
    """One connect per DSN per process — but never a cached caller identity.

    The direct side is fixed for the life of a DSN, so re-probing it on every
    `POST /jobs` is a connect+auth round trip to recompute a constant. The
    caller's connection varies per request, so caching *that* would let the
    first request's identity vouch for every later one.
    """
    connects: list[str] = []

    def _counting_connect(dsn):
        connects.append(dsn)
        return _Connection((*TCP_A, True))

    monkeypatch.setattr(
        "reimagina.orchestration.database_identity.psycopg2.connect", _counting_connect
    )

    assert_same_domain_database(_Connection(TCP_A), "postgresql://direct")
    assert_same_domain_database(_Connection(TCP_A), "postgresql://direct")
    assert connects == ["postgresql://direct"], "direct probe re-ran"

    # A caller that has moved is still caught, cache or no cache.
    with pytest.raises(RuntimeError, match="different databases"):
        assert_same_domain_database(_Connection(TCP_B), "postgresql://direct")
