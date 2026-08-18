"""Unit tests for progress-event payloads and SSE framing — pure, no DB."""

from __future__ import annotations

import json

from reimagina.orchestration.frames import (
    event_frame,
    is_run_terminal,
    notify_payload,
    sse_frame,
)
from reimagina.orchestration.port import RUN_TERMINAL_STATES, RunEvent


def test_notify_payload_roundtrips():
    payload = notify_payload(run_id="r1", indicator="walkability", status="succeeded")
    assert json.loads(payload) == {
        "run_id": "r1",
        "indicator": "walkability",
        "status": "succeeded",
    }


def test_notify_payload_run_level_has_null_indicator():
    assert (
        json.loads(notify_payload(run_id="r1", indicator=None, status="partial"))[
            "indicator"
        ]
        is None
    )


def test_sse_frame_format():
    assert sse_frame('{"a":1}') == 'data: {"a":1}\n\n'


def test_is_run_terminal():
    assert is_run_terminal("succeeded")
    assert is_run_terminal("failed")
    assert is_run_terminal("partial")
    assert not is_run_terminal("running")
    assert not is_run_terminal("queued")


def test_is_run_terminal_tracks_the_port_vocabulary():
    """One definition: frames used to keep a private copy that could drift."""
    assert all(is_run_terminal(s) for s in RUN_TERMINAL_STATES)


def test_event_frame_renders_a_run_event():
    frame = event_frame(
        RunEvent(run_id="r1", indicator="walkability", status="running")
    )
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert json.loads(frame.removeprefix("data: ")) == {
        "run_id": "r1",
        "indicator": "walkability",
        "status": "running",
    }


def test_event_frame_omits_detail():
    """Deliberate: nothing consumes it, and GET /jobs/{run_id} already has it."""
    frame = event_frame(RunEvent("r1", "walkability", "failed", "boom"))
    assert "boom" not in frame
