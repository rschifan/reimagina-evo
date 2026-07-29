"""Shared job_run lifecycle for compute workers.

Collapses the identical ``start_job(running) → try → complete_job(succeeded)
/ except → complete_job(failed) → raise`` block that every worker (rollup,
building density, compactness, …) was hand-writing.

It also stamps each row with the orchestration run it belongs to, taken from an
ambient ``current_run`` rather than a parameter. That is deliberate: the run id
would otherwise have to be threaded through the ``Runner`` protocol, both
adapter closures, four compute-fn signatures and every test fake, to reach the
one place that writes a row — and a direct CLI compute, which belongs to no run,
would have to pass ``None`` through all of it. An executor sets it once around
the call; unset means "not part of an orchestrated run", which is a real case
and not an error.
"""

from __future__ import annotations

import logging
from contextlib import closing, contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from reimagina.db import connect
from reimagina.indicators.catalog import SHIPPED_INDICATORS
from reimagina.storage import backend_for_connection

logger = logging.getLogger(__name__)

# The orchestration run whose dirty-set the current compute belongs to, or None
# for a standalone job (a direct `indicators compute` from the CLI).
_current_run: ContextVar[str | None] = ContextVar("current_run", default=None)


@contextmanager
def current_run(run_id: str | None) -> Iterator[None]:
    """Attribute every ``job_run`` opened in this scope to ``run_id``.

    Set it *inside* whatever executes the unit, never around a dispatch: a
    ContextVar does not cross into a thread the executor did not itself enter,
    and this must be visible on the thread that runs the compute.
    """
    token = _current_run.set(run_id)
    try:
        yield
    finally:
        # A run id that outlived its run would mis-attribute the next job on
        # this thread — the reason a bare `.set()` is not enough.
        _current_run.reset(token)


@contextmanager
def job_run(
    backend: Any,
    *,
    model_family: str,
    code_version: str,
    scenario_id: str | None,
) -> Iterator[str]:
    """Open a ``job_run`` row, yield its id, and finalise its status.

    On clean exit the row is marked ``succeeded``; on any exception it is marked
    ``failed`` (with ``"<ExcType>: <msg>"`` as the error) and the exception is
    re-raised so the caller still sees the failure.

    The row records the ambient ``current_run`` and the backend pid of the
    connection doing the work, which together are what makes a run cancellable
    (see migration r031).
    """
    repo = backend.job_run_repo()
    job_run_id = repo.start_job(
        model_family=model_family,
        code_version=code_version,
        scenario_id=scenario_id,
        status="running",
        run_id=_current_run.get(),
    )
    try:
        yield job_run_id
    except Exception as exc:
        repo.complete_job(
            job_run_id,
            status="failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        repo.complete_job(job_run_id, status="succeeded")


def record_failed_job_run(
    dsn: str,
    *,
    run_id: str,
    indicator_slug: str,
    scenario_id: str | None,
    code_version: str,
    error: str,
) -> None:
    """Record a ``failed`` job_run for a unit whose transaction was rolled back.

    ``job_run`` above marks its row ``failed`` on the same connection that does
    the compute. An executor that rolls that connection back when a unit raises
    — which both ops-plane tracks do, because the half-written results have to
    go — takes the ``failed`` row with it, and a unit that ran for minutes and
    failed leaves no trace at all. ``job_run`` is the domain provenance record
    (F8), ``curated_geo.indicator_result.job_run_id`` is NOT NULL behind an
    existence trigger (r016), and the per-run timing query in the ops-plane
    runbook joins on it: a failure with no row is a failure nothing can see.

    So the row is written on a second connection, which the rollback cannot
    reach. Call this from the ``except`` block that does the rolling back — the
    caller is the only party that knows the erasure happened. The in-process
    coordinator, which keeps its connection and commits, must *not* call it: its
    row survives on its own, and a second one would double-count the failure.

    ``dsn`` is the compute connection's own (``conn.dsn``), never an environment
    lookup. Provenance written to a different database than the work it
    describes is worse than none.

    ``run_id`` is a parameter rather than the ambient ``current_run`` because
    this runs from an ``except``, after that scope has closed. Leaving it to the
    ambient would file every failed unit under a NULL run — invisible to
    ``cancel_run_backends`` and to the per-run query, the two things ``run_id``
    exists for.

    Best-effort by construction: an exception raised while recording a failure
    would replace the failure it is recording.

    ponytail: the row's *duration* is not recovered, only its existence. Both
    timestamps land at the moment this writes, so the runbook's
    ``sum(completed_at - started_at)`` counts a failed unit as roughly zero
    seconds however long it actually burned. ``count(*)`` — the part that made a
    failed unit invisible — is correct. Recovering the real elapsed time needs
    ``start_job`` to accept a ``started_at``, which is repo API surface this
    does not need; add it when failed-unit cost has to be quoted, and note that
    both tracks under-count it identically, so the comparison is unaffected.
    """
    spec = next((s for s in SHIPPED_INDICATORS if s.slug == indicator_slug), None)
    # Composites carry a NULL model_family (r016 CHECK) and their runner spells
    # that out as "composite". Resolved the same way here so the replacement row
    # reads like the one that was lost, not like a different kind of job.
    model_family = (spec.model_family or "composite") if spec else "unknown"
    try:
        # closing(), not `with managed_connection(...)`: psycopg2's connection context
        # manager scopes the transaction and leaves the socket open, and this
        # runs once per failed unit per run.
        with closing(connect(dsn)) as conn:
            repo = backend_for_connection(conn).job_run_repo()
            job_run_id = repo.start_job(
                model_family=model_family,
                code_version=code_version,
                scenario_id=scenario_id,
                status="running",
                run_id=run_id,
            )
            # The backend_pid this records is this connection's, not the one
            # that did the work. Harmless: the row is a record, and the backend
            # worth cancelling is already gone by the time we are here.
            repo.complete_job(job_run_id, status="failed", error_message=error)
            conn.commit()
    except Exception:  # noqa: BLE001 — provenance is best-effort
        logger.warning(
            "could not persist failed job_run for %s", indicator_slug, exc_info=True
        )
