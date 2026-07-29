"""Which unit failures Hatchet may retry, and which it must not.

`_is_retryable` decides whether a rolled-back unit gets another attempt. Each
attempt is free to burn `REIMAGINA_UNIT_TIMEOUT_MIN` (120 minutes by default),
so a wrong "yes" is expensive and a wrong "no" loses a recoverable unit.

These are pure classification tests: no engine, no database.
"""

from __future__ import annotations

import psycopg2

from reimagina.orchestration.hatchet_app import _is_retryable


def test_a_cancelled_statement_is_not_retryable():
    """The finding. `cancel_run` cancels a unit with `pg_cancel_backend`.

    ⚠ `QueryCanceled` inherits from `OperationalError`, so the broad
    connection-failure clause claims it unless cancellation is tested first.
    Classifying it retryable hands the engine a unit the operator just stopped
    and lets it run again — twice more, by default.
    """
    assert psycopg2.errors.QueryCanceled.__mro__[2] is psycopg2.OperationalError, (
        "the inheritance this test guards against has changed; re-check the order "
        "of the isinstance checks in _is_retryable"
    )
    assert _is_retryable(psycopg2.errors.QueryCanceled("canceling statement")) is False


def test_connection_failures_are_retryable():
    """A dropped connection says nothing about the work; try again."""
    assert _is_retryable(psycopg2.OperationalError("server closed the connection"))
    assert _is_retryable(psycopg2.InterfaceError("connection already closed"))


def test_serialization_failure_and_deadlock_are_retryable():
    for code in ("40001", "40P01"):
        exc = psycopg2.errors.lookup(code)("contention")
        assert _is_retryable(exc) is True, code


def test_a_deterministic_error_is_not_retryable():
    """Re-running a bug reproduces the bug, minutes later."""
    assert _is_retryable(RuntimeError("boom")) is False
    assert _is_retryable(ValueError("bad geometry")) is False
