"""Integration: NOTIFY round-trip + the SSE stream generator on a terminal run.

Uses committed connections (LISTEN/NOTIFY crosses connections) with explicit
cleanup, not the rollback ``conn`` fixture — but still takes the session
``migrated_db`` fixture, since skipping the rollback fixture also skips the
migration it performs. Without it this module only passed when some
alphabetically-earlier test happened to migrate first, which stopped being true
the moment it moved into ``engine/``.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
from uuid import uuid4

import psycopg2
import pytest

from reimagina.orchestration import events

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/reimagina_test")


def _skip_if_no_pg():
    try:
        psycopg2.connect(_DSN).close()
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")


def test_emit_delivers_over_listen_notify(migrated_db):
    _skip_if_no_pg()
    run_id = f"listen-{uuid4().hex[:8]}"
    listener = psycopg2.connect(_DSN)
    listener.autocommit = True
    emitter = psycopg2.connect(_DSN)
    emitter.autocommit = True
    try:
        with listener.cursor() as cur:
            cur.execute(f"LISTEN {events.CHANNEL}")
        events.emit(emitter, run_id=run_id, indicator="walkability", status="succeeded")
        assert select.select([listener], [], [], 3.0)[0], "no NOTIFY received"
        listener.poll()
        received = [json.loads(n.payload) for n in listener.notifies]
        assert any(
            p["run_id"] == run_id and p["status"] == "succeeded" for p in received
        )
    finally:
        listener.close()
        emitter.close()


def test_event_stream_emits_snapshot_then_closes_for_terminal_run(
    migrated_db, monkeypatch
):
    _skip_if_no_pg()
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    from apps.api.routers.jobs import _event_stream

    run_id = str(uuid4())
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs_runtime.orchestration_run "
                "(id, status, code_version) VALUES (%s, 'succeeded', 'test')",
                (run_id,),
            )

        # A terminal run → the generator yields one snapshot frame and returns
        # (it must not block waiting for NOTIFYs). The route drives the
        # orchestrator's async progress iterator, so it is an async generator —
        # this is the real LISTEN-backed one, not the unit tests' fake.
        async def _collect():
            return [chunk async for chunk in _event_stream(run_id)]

        frames = asyncio.run(_collect())
        assert len(frames) == 1
        payload = json.loads(frames[0].removeprefix("data: ").strip())
        assert payload == {"run_id": run_id, "indicator": None, "status": "succeeded"}
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s", (run_id,)
            )
        conn.close()


def test_subscribe_progress_streams_live_events_and_filters_other_runs(
    migrated_db, monkeypatch
):
    """Drive the port's LISTEN loop, which no other test reaches.

    Every other subscriber in the suite hits an already-terminal run and returns
    on the snapshot, so the notify path — the run_id filter and the
    terminal-event stop — ran nowhere. A regression in the filter leaks another
    run's progress into a client's SSE stream.
    """
    monkeypatch.setenv("DATABASE_URL_DIRECT", _DSN)
    from reimagina.orchestration.procrastinate_orchestrator import (
        ProcrastinateOrchestrator,
    )

    run_id = str(uuid4())
    other_run = str(uuid4())
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs_runtime.orchestration_run "
                "(id, status, code_version) VALUES (%s, 'running', 'test')",
                (run_id,),
            )

        async def _drive():
            stream = ProcrastinateOrchestrator().subscribe_progress(run_id).__aiter__()
            first = await stream.__anext__()  # snapshot: running, so it stays open

            emitter = psycopg2.connect(_DSN)
            emitter.autocommit = True
            try:
                # A different run's event must not reach this subscriber.
                events.emit(
                    emitter, run_id=other_run, indicator="noise", status="succeeded"
                )
                events.emit(
                    emitter, run_id=run_id, indicator="compactness", status="succeeded"
                )
                events.emit(emitter, run_id=run_id, indicator=None, status="succeeded")
            finally:
                emitter.close()

            rest = [event async for event in stream]
            return first, rest

        first, rest = asyncio.run(asyncio.wait_for(_drive(), timeout=15.0))

        assert first.status == "running"
        assert [(e.indicator, e.status) for e in rest] == [
            ("compactness", "succeeded"),
            (None, "succeeded"),
        ], "the other run's event leaked in, or the terminal event did not stop it"
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s", (run_id,)
            )
        conn.close()
