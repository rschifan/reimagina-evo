"""Integration: NOTIFY round-trip + the SSE stream generator on a terminal run.

Uses committed connections (LISTEN/NOTIFY crosses connections) with explicit
cleanup, not the rollback ``conn`` fixture.
"""

from __future__ import annotations

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


def test_emit_delivers_over_listen_notify():
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


def test_event_stream_emits_snapshot_then_closes_for_terminal_run(monkeypatch):
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
        # (it must not block waiting for NOTIFYs).
        frames = list(_event_stream(run_id))
        assert len(frames) == 1
        payload = json.loads(frames[0].removeprefix("data: ").strip())
        assert payload == {"run_id": run_id, "indicator": None, "status": "succeeded"}
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM jobs_runtime.orchestration_run WHERE id = %s", (run_id,)
            )
        conn.close()
