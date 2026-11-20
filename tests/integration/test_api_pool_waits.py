"""A request that finds every pooled connection busy waits for one.

psycopg2's ThreadedConnectionPool never blocks: the 17th concurrent request
against a 16-connection pool raised ``PoolError("connection pool exhausted")``
and the tile came back 500. A map loading at a new zoom fires more than that
at once, so tiles went missing on an idle server (measured: 40 concurrent
tile requests, 16 x 200 and 24 x 500). Waiting turns the burst into a queue.
"""

from __future__ import annotations

import asyncio
import threading

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


async def _hold(seconds: float, started=None) -> None:
    gen = deps.get_db_conn()
    conn = await anext(gen)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        if started is not None:
            started.set()
        await asyncio.sleep(seconds)
    finally:
        await gen.aclose()


def test_more_requests_than_connections_all_get_one(tiny_pool):
    tiny_pool(2)

    async def requests():
        await asyncio.gather(*(_hold(0.02) for _ in range(6)))

    asyncio.run(requests())


def test_a_request_that_waits_too_long_is_told_to_retry(tiny_pool, monkeypatch):
    """Waiting is bounded: past the limit the answer is 503, not a hang."""
    monkeypatch.setenv("REIMAGINA_API_POOL_WAIT_S", "0.03")
    tiny_pool(1)

    async def requests():
        started = asyncio.Event()
        holder = asyncio.create_task(_hold(0.1, started))
        await started.wait()
        try:
            with pytest.raises(HTTPException) as caught:
                await _hold(0)
            assert caught.value.status_code == 503
            assert caught.value.headers == {"Retry-After": "1"}
        finally:
            await holder

    asyncio.run(requests())


def test_cancelling_a_waiter_does_not_lose_pool_capacity(tiny_pool):
    tiny_pool(1)

    async def requests():
        held = deps.get_db_conn()
        await anext(held)
        waiting = deps.get_db_conn()
        task = asyncio.create_task(anext(waiting))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await waiting.aclose()
        await held.aclose()
        await asyncio.wait_for(_hold(0), timeout=1)

    asyncio.run(requests())


def test_cancelling_a_request_rolls_back_and_returns_its_connection(tiny_pool):
    tiny_pool(1)

    async def requests():
        held = deps.get_db_conn()
        conn = await anext(held)
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE cancelled_request_probe (value integer)")
        with pytest.raises(asyncio.CancelledError):
            await held.athrow(asyncio.CancelledError())
        returned = deps.get_db_conn()
        conn = await asyncio.wait_for(anext(returned), timeout=1)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('pg_temp.cancelled_request_probe')")
                assert cur.fetchone()[0] is None
        finally:
            await returned.aclose()

    asyncio.run(requests())


@pytest.mark.parametrize("phase", ["checkout", "return"])
def test_cancellation_waits_for_inflight_database_operations(
    tiny_pool, monkeypatch, phase
):
    """Cancellation cannot abandon a thread while it owns a pool connection."""
    tiny_pool(1)
    entered, release, finished = (threading.Event() for _ in range(3))
    if phase == "checkout":
        owner, method = deps._pool, "getconn"
    else:
        owner, method = deps, "_return_connection"
    operation = getattr(owner, method)

    def slow_operation(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(timeout=3)
            return operation(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(owner, method, slow_operation)

    async def request():
        gen = deps.get_db_conn()
        if phase == "checkout":
            task = asyncio.create_task(anext(gen))
        else:
            await anext(gen)
            task = asyncio.create_task(gen.aclose())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done(), (
                "capacity was released while database I/O was running"
            )
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(finished.wait, 2)
        monkeypatch.setattr(owner, method, operation)
        await asyncio.wait_for(_hold(0), timeout=1)

    asyncio.run(request())


def test_waiting_for_connections_does_not_starve_request_handlers(
    tiny_pool, monkeypatch
):
    """A tile burst must leave threads available to use checked-out connections."""
    import anyio
    import httpx
    from fastapi import Depends, FastAPI

    tiny_pool(1)
    monkeypatch.setenv("REIMAGINA_API_POOL_WAIT_S", "0.3")
    app = FastAPI()

    @app.get("/read")
    def read(conn=Depends(deps.get_db_conn)):
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return {"value": cur.fetchone()[0]}

    async def burst():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 2
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                responses = await asyncio.gather(
                    *(client.get("/read") for _ in range(8))
                )
            assert [r.status_code for r in responses] == [200] * 8
            assert all(r.json() == {"value": 1} for r in responses)
        finally:
            limiter.total_tokens = previous

    asyncio.run(burst())
