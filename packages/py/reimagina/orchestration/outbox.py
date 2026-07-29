"""Transactional scenario-edit outbox and Hatchet event delivery."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    scenario_id: str
    changed_attributes: tuple[str, ...]
    edited_features: tuple[str, ...]
    claim_token: str


def enqueue_scenario_edit(
    conn: Any,
    *,
    scenario_id: str,
    changed_attributes: Iterable[str],
    edited_features: Iterable[str],
) -> None:
    """Write a delivery record in the same transaction as the domain edit."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs_runtime.orchestration_outbox
              (scenario_id, changed_attributes, edited_features)
            VALUES (%s, %s::text[], %s::uuid[])
            """,
            (scenario_id, sorted(set(changed_attributes)), list(edited_features)),
        )


def claim_pending(
    conn: Any, *, limit: int = 100, lease_seconds: int = 900
) -> list[OutboxEvent]:
    """Lease pending records without holding a DB transaction during RPC.

    The engine call cannot share this database transaction, so delivery is
    necessarily at-least-once. The lease token prevents a stale publisher from
    acknowledging a row that a recovery worker has already reclaimed.
    """
    claim_token = str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH candidates AS (
                SELECT id
                FROM jobs_runtime.orchestration_outbox
                WHERE delivered_at IS NULL
                  AND (
                    claimed_at IS NULL
                    OR claimed_at < clock_timestamp() - make_interval(secs => %s)
                  )
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE jobs_runtime.orchestration_outbox AS outbox
            SET claimed_at = clock_timestamp(),
                claim_token = %s::uuid,
                attempts = attempts + 1,
                error_message = NULL
            FROM candidates
            WHERE outbox.id = candidates.id
            RETURNING outbox.id::text, outbox.scenario_id::text,
                      outbox.changed_attributes, outbox.edited_features::text[],
                      outbox.claim_token::text
            """,
            (lease_seconds, limit, claim_token),
        )
        return [
            OutboxEvent(row[0], row[1], tuple(row[2]), tuple(row[3]), row[4])
            for row in cur.fetchall()
        ]


def mark_delivered(conn: Any, event_id: str, claim_token: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs_runtime.orchestration_outbox
            SET delivered_at = clock_timestamp(),
                claimed_at = NULL,
                claim_token = NULL,
                error_message = NULL
            WHERE id = %s::uuid AND claim_token = %s::uuid
            """,
            (event_id, claim_token),
        )


def mark_failed(
    conn: Any, event_id: str, claim_token: str, error: BaseException
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs_runtime.orchestration_outbox
            SET claimed_at = NULL, claim_token = NULL, error_message = %s
            WHERE id = %s::uuid AND claim_token = %s::uuid AND delivered_at IS NULL
            """,
            (f"{type(error).__name__}: {error}"[:2000], event_id, claim_token),
        )
