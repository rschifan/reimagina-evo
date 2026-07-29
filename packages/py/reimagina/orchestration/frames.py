"""Progress-event payloads and SSE framing — engine-agnostic.

Split out of ``events.py`` so the two halves can diverge: ``events`` is the
Postgres LISTEN/NOTIFY transport and dies with the Procrastinate ops-plane,
while everything here is pure formatting that every ops-plane implementation
needs. The API route formats whatever the orchestrator's ``subscribe_progress``
yields, so this module — not the engine — owns the wire shape the web app sees.
"""

from __future__ import annotations

import json

from reimagina.orchestration.port import RUN_TERMINAL_STATES, RunEvent


def notify_payload(*, run_id: str, indicator: str | None, status: str) -> str:
    """The JSON body of one progress event.

    ``indicator=None`` marks the run-level event carrying the overall status,
    which tells a stream to close.

    ``RunEvent.detail`` is deliberately absent from the wire: no client reads it
    and ``GET /jobs/{run_id}`` already carries it per unit. Add it here the day
    something consumes it.
    """
    return json.dumps({"run_id": run_id, "indicator": indicator, "status": status})


def sse_frame(payload: str) -> str:
    """Format a JSON payload as one Server-Sent Events data frame."""
    return f"data: {payload}\n\n"


def event_frame(event: RunEvent) -> str:
    """Format one orchestrator progress event as an SSE frame."""
    return sse_frame(
        notify_payload(
            run_id=event.run_id, indicator=event.indicator, status=event.status
        )
    )


def error_frame(reason: str) -> str:
    """A terminal SSE frame for a failure HTTP status can no longer express.

    Once a ``StreamingResponse`` has sent its headers the status code is settled,
    so an orchestrator outage mid-stream cannot become a 503 — the stream would
    simply stop, which a client cannot tell from a run that finished. This says
    so in the protocol's own vocabulary instead.

    ⚠ An SSE **event type**, deliberately not a new run status. ``RunEvent.status``
    is the port's vocabulary and ``RUN_TERMINAL_STATES`` is its closed set;
    widening either to carry a transport failure would push an API concern into
    the orchestration contract. A client that does not know ``error`` ignores the
    frame and sees the stream close — what it sees today, minus the ambiguity.

    ``reason`` is a fixed, non-sensitive token. Never pass exception text: it
    carries run ids and engine internals onto a public stream.
    """
    return f"event: error\ndata: {json.dumps({'reason': reason})}\n\n"


def is_run_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATES
