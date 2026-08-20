"""A failing job must report *its own* error, not the bookkeeping's.

The compute usually fails by aborting the transaction, and the ``job_run``
context manager then writes ``status='failed'`` on that same connection. Both
halves of that are hazards: the write cannot run until the transaction is rolled
back, and an exception raised inside an ``except`` block replaces the one being
handled. Together they turned a ``UniqueViolation`` naming the offending row
into ``InFailedSqlTransaction: current transaction is aborted`` — a message
about the reporting rather than the fault, shown to an operator in the run
detail as the reason their indicator failed.
"""

from __future__ import annotations

import pytest

from reimagina.jobs._lifecycle import job_run


class _AbortedTransaction(Exception):
    """Stand-in for psycopg2's InFailedSqlTransaction."""


class _FakeRepo:
    """A repo whose writes fail while the transaction is aborted."""

    def __init__(self) -> None:
        self.aborted = False
        self.completed: list[tuple[str, str | None]] = []

    # -- repo surface ------------------------------------------------------
    def start_job(self, **_kwargs) -> str:
        return "job-1"

    def complete_job(self, _job_run_id, *, status, error_message=None) -> None:
        if self.aborted:
            raise _AbortedTransaction("current transaction is aborted")
        self.completed.append((status, error_message))


class _FakeBackend:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    def job_run_repo(self) -> _FakeRepo:
        return self._repo


def _run(repo: _FakeRepo, exc: Exception, *, abort: bool = True):
    with job_run(
        _FakeBackend(repo),
        model_family="urban",
        code_version="test",
        scenario_id=None,
    ):
        repo.aborted = abort
        raise exc


def test_the_original_error_survives_an_aborted_transaction():
    """The caller must see the fault, not the failed status write."""
    repo = _FakeRepo()
    original = ValueError("duplicate key value violates unique constraint")

    with pytest.raises(ValueError) as excinfo:
        _run(repo, original)

    assert excinfo.value is original


def test_the_failure_is_recorded_when_the_transaction_is_usable():
    """The ordinary case: a compute that failed without poisoning its connection."""
    repo = _FakeRepo()

    with pytest.raises(ValueError):
        _run(repo, ValueError("boom"), abort=False)

    assert repo.completed == [("failed", "ValueError: boom")]


def test_an_unwritable_row_is_given_up_rather_than_masking_the_cause():
    """Provenance is recoverable; the error is not.

    `s_compute` re-records the failure on a fresh connection for exactly this
    reason, so dropping the row here costs nothing the operator can see.
    """
    repo = _FakeRepo()
    original = ValueError("the actual problem")

    with pytest.raises(ValueError) as excinfo:
        _run(repo, original)

    assert excinfo.value is original
    assert repo.completed == []


def test_a_clean_run_is_marked_succeeded():
    repo = _FakeRepo()

    with job_run(
        _FakeBackend(repo),
        model_family="urban",
        code_version="test",
        scenario_id=None,
    ):
        pass

    assert repo.completed == [("succeeded", None)]
