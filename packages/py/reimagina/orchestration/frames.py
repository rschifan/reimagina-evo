"""Progress-event payloads and SSE framing — engine-agnostic.

Pure formatting, deliberately separate from any transport. It was split from
the LISTEN/NOTIFY module that has since been deleted with the queue ops-plane —
and surviving that deletion untouched is the point: the API route formats
whatever the orchestrator's ``subscribe_progress`` yields, so this module, not
the engine, owns the wire shape the web app sees.
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


def is_run_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATES
