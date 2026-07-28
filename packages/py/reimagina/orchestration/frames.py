"""Progress-event payloads and SSE framing — engine-agnostic.

Split out of ``events.py`` so the two halves can diverge: ``events`` is the
Postgres LISTEN/NOTIFY transport and dies with the Procrastinate ops-plane,
while everything here is pure formatting that every ops-plane implementation
needs. The API route formats whatever the orchestrator's ``subscribe_progress``
yields, so this module — not the engine — owns the wire shape the web app sees.
"""

from __future__ import annotations

import json

_RUN_TERMINAL = frozenset({"succeeded", "partial", "failed", "cancelled"})


def notify_payload(*, run_id: str, indicator: str | None, status: str) -> str:
    """The JSON body of one progress event.

    ``indicator=None`` marks the run-level event carrying the overall status,
    which tells a stream to close.
    """
    return json.dumps({"run_id": run_id, "indicator": indicator, "status": status})


def sse_frame(payload: str) -> str:
    """Format a JSON payload as one Server-Sent Events data frame."""
    return f"data: {payload}\n\n"


def is_run_terminal(status: str) -> bool:
    return status in _RUN_TERMINAL
