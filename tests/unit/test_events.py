"""Unit tests for run telemetry event formatting — pure, no DB."""

from __future__ import annotations

import json

from reimagina.orchestration.events import (
    is_run_terminal,
    notify_payload,
    sse_frame,
)


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
