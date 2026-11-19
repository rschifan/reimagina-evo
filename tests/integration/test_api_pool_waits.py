"""A request that finds every pooled connection busy waits for one.

psycopg2's ThreadedConnectionPool never blocks: the 17th concurrent request
against a 16-connection pool raised ``PoolError("connection pool exhausted")``
and the tile came back 500. A map loading at a new zoom fires more than that
at once, so tiles went missing on an idle server (measured: 40 concurrent
tile requests, 16 x 200 and 24 x 500). Waiting turns the burst into a queue.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException

from apps.api import deps


@pytest.fixture
def tiny_pool(monkeypatch, migrated_db):
    """A pool of ``n`` connections, swapped in for whatever the session holds."""
    monkeypatch.setattr(deps, "_pool", None)
    monkeypatch.setattr(deps, "_slots", None)

    def make(n: int) -> None:
        deps.initialise_pool(dsn=migrated_db, minconn=1, maxconn=n)

    yield make
    deps.shutdown_pool()


def _hold(seconds: float) -> None:
    gen = deps.get_db_conn()
    conn = next(gen)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    time.sleep(seconds)
    gen.close()


def test_more_requests_than_connections_all_get_one(tiny_pool):
    tiny_pool(2)
    errors: list[BaseException] = []

    def request() -> None:
        try:
            _hold(0.2)
        except BaseException as exc:  # noqa: BLE001 - the test reports any
            errors.append(exc)

    threads = [threading.Thread(target=request) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_a_request_that_waits_too_long_is_told_to_retry(tiny_pool, monkeypatch):
    """Waiting is bounded: past the limit the answer is 503, not a hang."""
    monkeypatch.setenv("REIMAGINA_API_POOL_WAIT_S", "0.2")
    tiny_pool(1)
    holder = threading.Thread(target=_hold, args=(1.0,))
    holder.start()
    time.sleep(0.1)
    try:
        with pytest.raises(HTTPException) as caught:
            _hold(0)
        assert caught.value.status_code == 503
        assert caught.value.headers == {"Retry-After": "1"}
    finally:
        holder.join()
